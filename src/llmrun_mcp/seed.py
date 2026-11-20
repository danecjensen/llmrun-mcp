"""Find good public agent skills and turn them into llmrun seed skills.

The pipeline is: clone each source repo (shallow, cached), find every
SKILL.md, work out the license that covers it, check quality, normalize to
llmrun's format (with an attribution footer), dedupe, then either write a
preview or load the skills into a local SQLite file or a server.

Nothing here talks to GitHub's API, so it works without a token and without
rate limits: licenses are read from the LICENSE files themselves and
freshness comes from the newest commit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db, skillmd

# ---------------------------------------------------------------- sources


@dataclass(frozen=True)
class Source:
    repo: str  # "owner/name" on GitHub, or a local directory
    subdir: str = ""  # only look for skills under this folder
    note: str = ""

    @property
    def label(self) -> str:
        return self.repo if not Path(self.repo).exists() else Path(self.repo).name


# Ordered by priority: when two sources ship a skill with the same name, the
# earlier source wins. Licenses noted here are what the repos said on
# 2026-11-20; the script re-reads them on every run.
DEFAULT_SOURCES: list[Source] = [
    Source("anthropics/skills", "skills", "Anthropic's public skills; most Apache-2.0, document skills are not open source"),
    Source("obra/superpowers", "skills", "MIT; software-development workflow skills (TDD, debugging, planning)"),
    Source("addyosmani/agent-skills", "", "MIT; production engineering skills"),
    Source("openai/skills", "skills/.curated", "per-skill licenses; curated Codex skills"),
    Source("huggingface/skills", "", "Apache-2.0; ML and Hugging Face Hub skills"),
    Source("cloudflare/skills", "", "Apache-2.0; Workers, D1, R2 and friends"),
    Source("getsentry/skills", "", "Apache-2.0; Sentry's engineering skills"),
    Source("supabase/agent-skills", "", "MIT; Postgres and Supabase skills"),
    Source("coreyhaines31/marketingskills", "", "MIT; CRO, copywriting, SEO"),
    Source("trailofbits/skills", "", "CC-BY-SA-4.0 (share-alike, off by default); security review skills"),
    Source("vercel-labs/agent-skills", "skills", "no license file at last look, so skipped unless one appears"),
]

DEFAULT_LICENSES = (
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "0BSD",
    "CC0-1.0",
    "CC-BY-4.0",
    "Unlicense",
)


def parse_source(spec: str) -> Source:
    """`owner/repo`, `owner/repo:sub/dir`, or a local folder."""
    if Path(spec).expanduser().exists():
        return Source(str(Path(spec).expanduser().resolve()))
    repo, _, subdir = spec.partition(":")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError(f"source {spec!r}: expected owner/repo[:subdir] or a local folder")
    for s in DEFAULT_SOURCES:
        if s.repo == repo and not subdir:
            return s
    return Source(repo, subdir.strip("/"))


# ---------------------------------------------------------------- fetching


@dataclass
class Checkout:
    path: Path
    url: str  # browsable base, e.g. https://github.com/o/r/tree/<sha>
    commit: str | None
    committed_at: str | None


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=600
    ).stdout.strip()


def fetch(source: Source, cache_dir: Path, refresh: bool = True) -> Checkout:
    local = Path(source.repo)
    if local.exists():
        path = local
    else:
        path = cache_dir / source.repo.replace("/", "__")
        remote = f"https://github.com/{source.repo}.git"
        if (path / ".git").exists():
            if refresh:
                _git("fetch", "--depth", "1", "origin", "HEAD", cwd=path)
                _git("reset", "--hard", "FETCH_HEAD", cwd=path)
        else:
            if path.exists():
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _git("clone", "--quiet", "--depth", "1", remote, str(path))
    commit = committed_at = None
    try:
        commit = _git("rev-parse", "HEAD", cwd=path)
        committed_at = _git("log", "-1", "--format=%cI", cwd=path)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    if local.exists():
        url = path.as_uri()
    else:
        url = f"https://github.com/{source.repo}/tree/{commit or 'HEAD'}"
    return Checkout(path, url, commit, committed_at)


# ---------------------------------------------------------------- licenses

_LICENSE_NAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENCE", "COPYING", "LICENSE.TXT")

# (spdx, pattern). Order matters: more specific texts first.
_LICENSE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AGPL-3.0", re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE", re.I)),
    ("LGPL-3.0", re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE", re.I)),
    ("GPL-3.0", re.compile(r"GNU GENERAL PUBLIC LICENSE", re.I)),
    ("MPL-2.0", re.compile(r"Mozilla Public License", re.I)),
    ("Apache-2.0", re.compile(r"Apache License,?\s+(Version\s+)?2\.0|\bApache-2\.0\b", re.I)),
    ("CC-BY-SA-4.0", re.compile(r"Attribution[- ]ShareAlike 4\.0", re.I)),
    ("CC-BY-NC-4.0", re.compile(r"Attribution[- ]NonCommercial", re.I)),
    ("CC-BY-4.0", re.compile(r"Creative Commons Attribution 4\.0|CC[- ]BY[- ]4\.0", re.I)),
    ("CC0-1.0", re.compile(r"CC0 1\.0|Creative Commons Zero", re.I)),
    ("Unlicense", re.compile(r"This is free and unencumbered software released into the public domain", re.I)),
    ("0BSD", re.compile(r"Permission to use, copy, modify, and/or distribute.*?WITHOUT WARRANTY", re.I | re.S)),
    ("ISC", re.compile(r"ISC License|Permission to use, copy, modify, and/or distribute this software", re.I)),
    ("BSD-3-Clause", re.compile(r"Redistribution and use in source and binary forms.*Neither the name", re.I | re.S)),
    ("BSD-2-Clause", re.compile(r"Redistribution and use in source and binary forms", re.I)),
    ("MIT", re.compile(r"Permission is hereby granted, free of charge|\bMIT License\b", re.I)),
]

# A real holder line ("Copyright (c) 2025 Jane Doe"), not license boilerplate.
_COPYRIGHT_RE = re.compile(r"^\s*(?:Copyright|©)\b[^\n]*\b(19|20)\d\d\b[^\n]*$", re.I | re.M)


def classify_license(text: str) -> str:
    for spdx, pat in _LICENSE_PATTERNS:
        if pat.search(text[:20000]):
            # 0BSD and ISC share wording; 0BSD has no "above copyright notice" clause.
            if spdx == "0BSD" and "above copyright notice" in text:
                continue
            return spdx
    return "unrecognized"


@dataclass
class LicenseInfo:
    spdx: str  # SPDX id, "unrecognized", or "none"
    file: str | None
    copyright: str | None


def find_license(skill_dir: Path, root: Path) -> LicenseInfo:
    """The nearest license file at or above the skill folder wins."""
    d = skill_dir
    while True:
        for name in _LICENSE_NAMES:
            f = d / name
            if f.is_file():
                text = f.read_text(encoding="utf-8", errors="replace")
                m = _COPYRIGHT_RE.search(text)
                holder = m.group(0).strip() if m and "[" not in m.group(0) else None
                return LicenseInfo(classify_license(text), str(f.relative_to(root)), holder)
        if d == root or root not in d.parents:
            return LicenseInfo("none", None, None)
        d = d.parent


# ---------------------------------------------------------------- quality

# Instructions that ask an agent to do something dangerous or to drop its
# guard. Skills are instructions other agents will follow, so these are
# rejected unless --allow-risky is passed.
_RISKY = [
    (re.compile(r"ignore (all |any )?(previous|prior|above) instructions", re.I), "tells the agent to ignore its instructions"),
    (re.compile(r"\brm\s+-rf\s+(/|~|\$HOME)(\s|$)"), "deletes a home or root folder"),
    (re.compile(r"base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh"), "runs decoded base64"),
    (re.compile(r"--dangerously-skip-permissions|bypassPermissions", re.I), "turns off permission prompts"),
    (re.compile(r"send (the |your |all )?(api keys?|tokens?|secrets|passwords?) to (?!the intended)", re.I), "sends secrets somewhere"),
]
# Worth a look but common in legitimate skills (official CLI installers).
_CAUTION = [
    (re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "pipes an installer into a shell"),
]


def _quoted(line: str, start: int) -> bool:
    """True when the match sits inside quotes, i.e. it's an example of what to watch for."""
    before = line[:start]
    return before.count('"') % 2 == 1 or before.count("“") > before.count("”") or before.count("`") % 2 == 1


def scan(body: str) -> tuple[list[str], list[str]]:
    risky, caution = [], []
    for line in body.splitlines():
        for pat, why in _RISKY:
            m = pat.search(line)
            if m and not _quoted(line, m.start()) and why not in risky:
                risky.append(why)
        for pat, why in _CAUTION:
            if pat.search(line) and why not in caution:
                caution.append(why)
    return risky, caution


_TEMPLATE_NAMES = {"template", "template-skill", "example", "example-skill", "skill-template", "my-skill"}
_REL_FILE_RE = re.compile(r"(?<![\w/.])((?:scripts|references|assets|templates|resources|examples)/[\w./-]+)")

MIN_DESCRIPTION = 40
MAX_DESCRIPTION = 1024
MIN_BODY = 400
MAX_BODY = 60_000


@dataclass
class Candidate:
    source: str
    path: str  # SKILL.md path inside the repo
    url: str
    name: str = ""
    description: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    license: str = "none"
    license_file: str | None = None
    copyright: str | None = None
    committed_at: str | None = None
    bundled_files: list[str] = field(default_factory=list)
    referenced_files: list[str] = field(default_factory=list)
    score: int = 0
    accepted: bool = False
    reasons: list[str] = field(default_factory=list)  # why rejected
    warnings: list[str] = field(default_factory=list)  # kept, but worth a look

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("body")
        d["body_chars"] = len(self.body)
        return d


_SCRIPT_EXT = (".py", ".sh", ".js", ".mjs", ".ts", ".rb", ".go", ".ps1")


def is_script(path: str) -> bool:
    return path.startswith("scripts/") or path.endswith(_SCRIPT_EXT)


def quality_score(c: Candidate) -> int:
    """0-100, used to order the preview and for --min-score."""
    s = 0
    s += min(25, len(c.description) // 6)  # a real "what and when" sentence
    s += 25 if 1500 <= len(c.body) <= 20000 else 15 if len(c.body) >= MIN_BODY else 0
    s += 10 if re.search(r"^#{1,3} ", c.body, re.M) else 0
    s += 10 if re.search(r"^\s*(\d+\.|[-*]) ", c.body, re.M) else 0
    s += 10 if "```" in c.body else 0
    s += 10 if re.search(r"\b(when to use|use this when|use when|don't use|do not use)\b", c.description + c.body[:2000], re.I) else 0
    s += 10 if not c.referenced_files else 0  # self-contained: works from the text alone
    return min(100, s)


def check(
    c: Candidate,
    *,
    licenses: set[str],
    max_age_days: int,
    allow_risky: bool,
    self_contained_only: bool,
    min_score: int,
    allow_scripts: bool = False,
    today: datetime | None = None,
) -> None:
    today = today or datetime.now(UTC)
    if c.license not in licenses:
        c.reasons.append(
            "no license file" if c.license == "none"
            else "license file isn't a known open license" if c.license == "unrecognized"
            else f"license {c.license} not in the allowed list"
        )
    if c.committed_at and max_age_days:
        age = (today - datetime.fromisoformat(c.committed_at)).days
        if age > max_age_days:
            c.reasons.append(f"source not updated in {age} days")
    if c.name in _TEMPLATE_NAMES or "/template" in c.path.lower():
        c.reasons.append("template or example skill")
    if len(c.description) < MIN_DESCRIPTION:
        c.reasons.append(f"description too short ({len(c.description)} chars)")
    if len(c.description) > MAX_DESCRIPTION:
        c.reasons.append(f"description too long ({len(c.description)} chars)")
    if len(c.body) < MIN_BODY:
        c.reasons.append(f"instructions too short ({len(c.body)} chars)")
    if len(c.body) > MAX_BODY:
        c.reasons.append(f"instructions too long ({len(c.body)} chars)")
    if not re.search(r"^(#{1,6} |\s*(\d+\.|[-*]) )", c.body, re.M):
        c.reasons.append("no headings or steps")
    risky, caution = scan(c.body)
    for why in risky:
        (c.warnings if allow_risky else c.reasons).append(f"risky: {why}")
    c.warnings += [f"check: {why}" for why in caution]
    scripts = [f for f in c.referenced_files if is_script(f)]
    docs = [f for f in c.referenced_files if not is_script(f)]
    if scripts:
        msg = f"needs {len(scripts)} helper script(s) not stored in llmrun, e.g. {scripts[0]}"
        (c.warnings if allow_scripts and not self_contained_only else c.reasons).append(msg)
    if docs:
        msg = f"points to {len(docs)} reference file(s) not stored in llmrun, e.g. {docs[0]}"
        (c.reasons if self_contained_only else c.warnings).append(msg)
    c.score = quality_score(c)
    if c.score < min_score:
        c.reasons.append(f"quality score {c.score} below {min_score}")
    c.accepted = not c.reasons


# ---------------------------------------------------------------- collect


def find_skill_files(root: Path, subdir: str = "") -> list[Path]:
    base = root / subdir if subdir else root
    if not base.exists():
        return []
    out = []
    for f in sorted(base.rglob("SKILL.md")):
        parts = set(f.relative_to(root).parts)
        if parts & {".git", "node_modules", "dist", "build", ".venv"}:
            continue
        out.append(f)
    return out


def collect(source: Source, checkout: Checkout) -> list[Candidate]:
    root = checkout.path.resolve()
    out = []
    for f in find_skill_files(root, source.subdir):
        rel = f.relative_to(root).as_posix()
        skill_dir = f.parent
        c = Candidate(
            source=source.label,
            path=rel,
            url=f"{checkout.url}/{skill_dir.relative_to(root).as_posix()}".rstrip("/."),
            committed_at=checkout.committed_at,
        )
        try:
            meta = skillmd.parse(f.read_text(encoding="utf-8"), fallback_name=skill_dir.name)
            c.name = db.slugify(meta["name"])
            c.description, c.body = meta["description"], meta["body"]
            c.tags = [t.lower() for t in meta["tags"]]
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't stop the run
            c.reasons.append(f"unreadable SKILL.md: {e}")
            out.append(c)
            continue
        lic = find_license(skill_dir, root)
        c.license, c.license_file, c.copyright = lic.spdx, lic.file, lic.copyright
        c.bundled_files = sorted(
            p.relative_to(skill_dir).as_posix()
            for p in skill_dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md" and p.name not in _LICENSE_NAMES
        )
        refs = {m.group(1).rstrip(".,:;)`'\"") for m in _REL_FILE_RE.finditer(c.body)}
        c.referenced_files = sorted(r for r in refs if (skill_dir / r).exists() or any(b.startswith(r) for b in c.bundled_files))
        out.append(c)
    return out


def dedupe(cands: list[Candidate]) -> None:
    """Same name from two sources: keep the first (sources are in priority order)."""
    seen: dict[str, Candidate] = {}
    for c in cands:
        if not c.accepted:
            continue
        if c.name in seen:
            c.accepted = False
            c.reasons.append(f"duplicate of {seen[c.name].source}:{seen[c.name].path}")
        else:
            seen[c.name] = c


# ---------------------------------------------------------------- normalize


def source_tag(label: str) -> str:
    return "src-" + db.slugify(label)


def to_skill(c: Candidate) -> dict[str, Any]:
    """The skill as llmrun stores it: attribution travels inside the body."""
    footer = [
        "",
        "---",
        "",
        f"Seeded from {c.url}",
        f"License: {c.license}" + (f". {c.copyright}" if c.copyright else ""),
    ]
    if c.referenced_files:
        footer.append(
            f"This skill uses helper files that aren't stored here ({', '.join(c.referenced_files[:5])}); "
            "get them from the source link."
        )
    tags = sorted(set(c.tags) | {"seed", source_tag(c.source), "license-" + c.license.lower()})
    if c.referenced_files:
        tags.append("needs-bundled-files")
    return {
        "name": c.name,
        "description": c.description,
        "body": c.body.rstrip() + "\n" + "\n".join(footer),
        "tags": tags,
        "version": 1,
    }


# ---------------------------------------------------------------- output


def write_preview(out: Path, cands: list[Candidate], sources: list[dict[str, Any]]) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    skills_dir = out / "skills"
    if skills_dir.exists():
        shutil.rmtree(skills_dir)
    accepted = sorted((c for c in cands if c.accepted), key=lambda c: (-c.score, c.name))
    rejected = [c for c in cands if not c.accepted]
    for c in accepted:
        d = skills_dir / c.name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(skillmd.render(to_skill(c)), encoding="utf-8")

    report = {
        "generated_at": db.now(),
        "sources": sources,
        "counts": {"found": len(cands), "accepted": len(accepted), "rejected": len(rejected)},
        "accepted": [c.summary() for c in accepted],
        "rejected": [c.summary() for c in rejected],
    }
    (out / "preview.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "# llmrun seed preview",
        "",
        f"Generated {report['generated_at']}. Found {len(cands)} skills, "
        f"{len(accepted)} pass, {len(rejected)} rejected.",
        "",
        "## Sources",
        "",
        "| Source | Commit | Last commit | Found | Accepted | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in sources:
        lines.append(
            f"| {s['source']} | {(s.get('commit') or '')[:7]} | {(s.get('committed_at') or '')[:10]} "
            f"| {s['found']} | {s['accepted']} | {s.get('error') or s.get('note', '')} |"
        )
    lines += ["", "## Accepted (best first)", "", "| Score | Skill | Source | License | Notes |", "| --- | --- | --- | --- | --- |"]
    for c in accepted:
        lines.append(f"| {c.score} | [{c.name}]({c.url}) | {c.source} | {c.license} | {'; '.join(c.warnings)} |")
    lines += ["", "## Rejected", "", "| Skill | Source | Why |", "| --- | --- | --- |"]
    for c in sorted(rejected, key=lambda c: (c.source, c.name or c.path)):
        lines.append(f"| {c.name or c.path} | {c.source} | {'; '.join(c.reasons)} |")
    (out / "preview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def load_into_db(db_path: str, cands: list[Candidate]) -> list[tuple[str, dict[str, Any]]]:
    conn = db.connect(db_path)
    results = []
    for c in cands:
        s = to_skill(c)
        res = db.upsert_skill(
            conn,
            name=s["name"],
            description=s["description"],
            body=s["body"],
            tags=s["tags"],
            source=c.url,
            change_note="seeded from " + c.url,
        )
        results.append((c.name, res))
    conn.close()
    return results


# ---------------------------------------------------------------- run


def run(
    sources: list[Source],
    *,
    cache_dir: Path,
    licenses: set[str],
    max_age_days: int = 365,
    allow_risky: bool = False,
    self_contained_only: bool = False,
    min_score: int = 0,
    allow_scripts: bool = False,
    refresh: bool = True,
    log=print,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    all_cands: list[Candidate] = []
    source_report = []
    for src in sources:
        entry: dict[str, Any] = {"source": src.label, "subdir": src.subdir, "note": src.note, "found": 0, "accepted": 0}
        try:
            co = fetch(src, cache_dir, refresh=refresh)
            entry.update(commit=co.commit, committed_at=co.committed_at, url=co.url)
            cands = collect(src, co)
        except Exception as e:  # noqa: BLE001 - a missing repo shouldn't stop the others
            entry["error"] = f"fetch failed: {e}"
            log(f"skip {src.label}: {e}")
            source_report.append(entry)
            continue
        for c in cands:
            if not c.reasons:  # parse errors already rejected it
                check(
                    c,
                    licenses=licenses,
                    max_age_days=max_age_days,
                    allow_risky=allow_risky,
                    self_contained_only=self_contained_only,
                    min_score=min_score,
                    allow_scripts=allow_scripts,
                )
        all_cands.extend(cands)
        entry["found"] = len(cands)
        source_report.append(entry)
        log(f"{src.label:32} {len(cands):3} skills")
    dedupe(all_cands)
    by_source: dict[str, int] = {}
    for c in all_cands:
        if c.accepted:
            by_source[c.source] = by_source.get(c.source, 0) + 1
    for e in source_report:
        e["accepted"] = by_source.get(e["source"], 0)
    return all_cands, source_report
