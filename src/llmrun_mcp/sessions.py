"""Turn local Claude Code and Codex session logs into llmrun traces.

Claude Code keeps one JSONL file per session under ~/.claude/projects/<project>/.
Codex keeps "rollout" JSONL files under ~/.codex/sessions/YYYY/MM/DD/.

Everything that leaves this module goes through `scrub()` first, which
redacts common secret formats and replaces the home directory with `~`.
The outcome is always "unknown": a log can't tell us whether the user was
happy, so reviewers (human or AI) judge that on the server.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

CLAUDE_DIR = Path("~/.claude/projects").expanduser()
CODEX_DIR = Path("~/.codex/sessions").expanduser()

MAX_STEPS = 80
MAX_TRANSCRIPT = 120_000
MAX_TOOL_OUTPUT = 1_500

# ---------------------------------------------------------------- secrets

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S)),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai-key", re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("stripe-key", re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # scheme://user:password@host
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@'\"]+:[^/\s@'\"]+(?=@)")),
]
# Bearer <token>, and NAME=value / "name": "value" where the name looks secret.
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{12,}")
_SECRET_NAME = r"[A-Za-z0-9_\-]*(?:secret|token|passw(?:or)?d|passwd|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|credential|authorization)[A-Za-z0-9_\-]*"
_ASSIGNMENT = re.compile(
    r"(?i)(\b" + _SECRET_NAME + r"[\"']?\s*[:=]\s*[\"']?)"  # name =  /  "name": "
    r"(?!\$|\{\{|<|REDACTED|\*\*\*|true\b|false\b|null\b|none\b|\d+\b)"  # leave $VARS, templates, booleans alone
    r"([^\s\"',;}{)]{8,})"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def make_scrubber(extra_secrets: list[str] | None = None, home: str | None = None, emails: bool = True):
    """Return scrub(text) -> text with secrets redacted and the home dir shortened to ~."""
    home = home if home is not None else os.path.expanduser("~")
    literals = sorted({s for s in (extra_secrets or []) if s and len(s) >= 6}, key=len, reverse=True)

    def scrub(text: str) -> str:
        for lit in literals:
            text = text.replace(lit, "[REDACTED]")
        for label, pat in _SECRET_PATTERNS:
            text = pat.sub(f"[REDACTED:{label}]", text)
        text = _BEARER.sub(r"\1[REDACTED]", text)
        text = _ASSIGNMENT.sub(r"\1[REDACTED]", text)
        if emails:
            text = _EMAIL.sub("[email]", text)
        if home and home not in ("/", ""):
            text = text.replace(home, "~")
        return text

    return scrub


def scrub_obj(obj: Any, scrub) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x, scrub) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v, scrub) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- model


@dataclass
class Session:
    source: str  # "claude-code" | "codex"
    path: Path
    session_id: str
    cwd: str | None = None
    git_branch: str | None = None
    model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    prompts: list[str] = None  # type: ignore[assignment]
    steps: list[dict[str, Any]] = None  # type: ignore[assignment]
    transcript: list[str] = None  # type: ignore[assignment]
    last_reply: str | None = None
    tokens: int | None = None

    def __post_init__(self):
        self.prompts = self.prompts or []
        self.steps = self.steps or []
        self.transcript = self.transcript or []

    def to_trace(self) -> dict[str, Any]:
        """Arguments for the publish_trace tool (not yet scrubbed)."""
        goal = _clip(self.prompts[0], 600) if self.prompts else "(no user prompt found)"
        tools = Counter(s.get("tool", "?") for s in self.steps)
        top = ", ".join(f"{n} x{c}" for n, c in tools.most_common(6)) or "none"
        project = Path(self.cwd).name if self.cwd else "unknown project"
        parts = [
            f"{_AGENT_LABEL[self.source]} session in {project}"
            + (f" (branch {self.git_branch})" if self.git_branch else "")
            + f": {len(self.prompts)} user prompt(s), {len(self.steps)} tool call(s) ({top})."
        ]
        if len(self.prompts) > 1:
            parts.append("Follow-up prompts: " + " | ".join(_clip(p, 160) for p in self.prompts[1:4]))
        if self.last_reply:
            parts.append("Final reply: " + _clip(self.last_reply, 700))
        transcript = "\n\n".join(self.transcript)
        if len(transcript) > MAX_TRANSCRIPT:
            transcript = transcript[:MAX_TRANSCRIPT] + f"\n\n[... transcript truncated, {len(transcript)} chars total]"
        meta: dict[str, Any] = {
            "source": self.source,
            "session_id": self.session_id,
            "project": project,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_min": _duration_min(self.started_at, self.ended_at),
            "user_prompts": len(self.prompts),
            "tool_calls": len(self.steps),
            "tokens": self.tokens,
            "imported_by": "llmrun-mcp publish-sessions",
        }
        steps = self.steps
        if len(steps) > MAX_STEPS:
            steps = steps[: MAX_STEPS // 2] + [{"action": f"... {len(steps) - MAX_STEPS} steps omitted ..."}] + steps[-MAX_STEPS // 2 :]
        return {
            "goal": goal,
            "summary": "\n\n".join(parts),
            "outcome": "unknown",
            "steps": steps,
            "transcript": transcript,
            "agent": self.source,
            "model": self.model,
            "metadata": {k: v for k, v in meta.items() if v is not None},
        }


_AGENT_LABEL = {"claude-code": "Claude Code", "codex": "Codex"}


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _duration_min(a: str | None, b: str | None) -> float | None:
    try:
        d = datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))  # type: ignore[union-attr]
        return round(d.total_seconds() / 60, 1)
    except Exception:  # noqa: BLE001
        return None


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _short(v: Any, n: int = 300) -> str:
    if not isinstance(v, str):
        v = json.dumps(v, ensure_ascii=False)
    return _clip(v, n)


# Text that harnesses inject as "user" messages but the person didn't type.
_INJECTED_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command-stdout>", "<local-command-stderr>",
    "<system-reminder>", "Caveat: The messages below", "<environment_context>", "<user_instructions>",
    "# AGENTS.md instructions", "<user_shell_command>", "[Request interrupted", "<session-context",
    "This session is being continued from a previous conversation",
)


def _is_real_prompt(text: str) -> bool:
    t = text.lstrip()
    return bool(t) and not t.startswith(_INJECTED_PREFIXES)


def _slash_command(text: str) -> str | None:
    """Claude Code records `/foo args` as <command-name>/foo</command-name><command-args>args</...>."""
    m = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if not m:
        return None
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    return (m.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()


def _tool_detail(name: str, inp: Any) -> str:
    if isinstance(inp, dict):
        for key in ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
            if key in inp:
                v = inp[key]
                return _short(" ".join(v) if isinstance(v, list) else v)
    return _short(inp)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else ("[image]" if isinstance(b, dict) and b.get("type") == "image" else "")
            for b in content
        )
    return "" if content is None else json.dumps(content)


# ---------------------------------------------------------------- claude code


def parse_claude(path: Path) -> Session | None:
    s = Session(source="claude-code", path=path, session_id=path.stem)
    pending: dict[str, dict[str, Any]] = {}
    models: Counter[str] = Counter()
    tokens = 0
    for o in _jsonl(path):
        typ = o.get("type")
        ts = o.get("timestamp")
        if typ == "summary" or typ not in ("user", "assistant"):
            continue
        if o.get("isSidechain"):
            continue
        if ts:
            s.started_at = s.started_at or ts
            s.ended_at = ts
        s.session_id = o.get("sessionId") or s.session_id
        s.cwd = o.get("cwd") or s.cwd
        s.git_branch = o.get("gitBranch") or s.git_branch
        msg = o.get("message") or {}
        content = msg.get("content")
        if typ == "user":
            if o.get("isMeta"):
                continue
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text = b.get("text", "")
                    cmd = _slash_command(text)
                    if cmd:
                        s.prompts.append(cmd)
                        s.transcript.append(f"## User\n{cmd}")
                    elif _is_real_prompt(text):
                        s.prompts.append(text.strip())
                        s.transcript.append(f"## User\n{text.strip()}")
                elif b.get("type") == "tool_result":
                    out = _result_text(b.get("content"))
                    step = pending.pop(b.get("tool_use_id", ""), None)
                    if step is not None:
                        step["result"] = ("ERROR: " if b.get("is_error") else "") + _short(out, 200)
                    s.transcript.append(f"### Result{' (error)' if b.get('is_error') else ''}\n{_clip(out, MAX_TOOL_OUTPUT)}")
        else:
            if msg.get("model") and msg["model"] != "<synthetic>":
                models[msg["model"]] += 1
            usage = msg.get("usage") or {}
            tokens += sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
            for b in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    s.last_reply = b["text"].strip()
                    s.transcript.append(f"## Assistant\n{s.last_reply}")
                elif b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    step = {"action": "tool call", "tool": name, "detail": _tool_detail(name, b.get("input"))}
                    s.steps.append(step)
                    pending[b.get("id", "")] = step
                    s.transcript.append(f"### Tool: {name}\n{_clip(json.dumps(b.get('input'), ensure_ascii=False, indent=1), MAX_TOOL_OUTPUT)}")
    s.model = models.most_common(1)[0][0] if models else None
    s.tokens = tokens or None
    return s if s.prompts else None


# ---------------------------------------------------------------- codex


def parse_codex(path: Path) -> Session | None:
    s = Session(source="codex", path=path, session_id=path.stem.removeprefix("rollout-"))
    pending: dict[str, dict[str, Any]] = {}
    for o in _jsonl(path):
        ts = o.get("timestamp")
        typ = o.get("type")
        if "payload" in o and isinstance(o["payload"], dict):
            p = o["payload"]
            if typ == "session_meta":
                s.session_id = p.get("id") or s.session_id
                s.cwd = p.get("cwd") or s.cwd
                s.git_branch = (p.get("git") or {}).get("branch") or s.git_branch
                s.started_at = s.started_at or p.get("timestamp") or ts
                continue
            if typ == "turn_context":
                s.model = p.get("model") or s.model
                s.cwd = s.cwd or p.get("cwd")
                continue
            if typ == "event_msg":
                if p.get("type") == "token_count":
                    total = ((p.get("info") or {}).get("total_token_usage") or {}).get("total_tokens")
                    s.tokens = total or s.tokens
                continue
            if typ != "response_item":
                continue
            item = p
        else:
            # Older rollouts: first line is session meta, then bare items.
            if "instructions" in o and "id" in o and typ is None:
                s.session_id = o.get("id") or s.session_id
                s.git_branch = (o.get("git") or {}).get("branch") or s.git_branch
                s.started_at = s.started_at or o.get("timestamp")
                continue
            if o.get("record_type"):
                continue
            item = o
        if ts:
            s.started_at = s.started_at or ts
            s.ended_at = ts
        _codex_item(s, item, pending)
    return s if s.prompts else None


def _codex_item(s: Session, item: dict[str, Any], pending: dict[str, dict[str, Any]]) -> None:
    it = item.get("type")
    if it == "message":
        text = "\n".join(
            c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")
        ).strip()
        if not text:
            return
        if item.get("role") == "user":
            if _is_real_prompt(text):
                s.prompts.append(text)
                s.transcript.append(f"## User\n{text}")
        elif item.get("role") == "assistant":
            s.last_reply = text
            s.transcript.append(f"## Assistant\n{text}")
    elif it in ("function_call", "custom_tool_call", "local_shell_call"):
        name = item.get("name") or ("shell" if it == "local_shell_call" else "?")
        raw = item.get("arguments") if it == "function_call" else (item.get("input") if it == "custom_tool_call" else item.get("action"))
        args: Any = raw
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = raw
        step = {"action": "tool call", "tool": name, "detail": _tool_detail(name, args)}
        s.steps.append(step)
        pending[item.get("call_id") or item.get("id") or ""] = step
        shown = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=1)
        s.transcript.append(f"### Tool: {name}\n{_clip(shown or '', MAX_TOOL_OUTPUT)}")
    elif it in ("function_call_output", "custom_tool_call_output"):
        out = item.get("output")
        if isinstance(out, dict):
            out = out.get("content") or json.dumps(out)
        if isinstance(out, str):
            try:
                parsed = json.loads(out)
                if isinstance(parsed, dict) and "output" in parsed:
                    out = parsed["output"]
            except json.JSONDecodeError:
                pass
        out = _result_text(out)
        step = pending.pop(item.get("call_id", ""), None)
        if step is not None:
            step["result"] = _short(out, 200)
        s.transcript.append(f"### Result\n{_clip(out, MAX_TOOL_OUTPUT)}")


# ---------------------------------------------------------------- discovery


def find_session_files(claude_dir: Path = CLAUDE_DIR, codex_dir: Path = CODEX_DIR, sources: str = "all") -> list[tuple[str, Path]]:
    """All session files, newest first by modification time."""
    found: list[tuple[str, Path]] = []
    if sources in ("all", "claude") and claude_dir.is_dir():
        # Top-level <project>/<session>.jsonl only; subagent logs live deeper.
        found += [("claude-code", p) for p in claude_dir.glob("*/*.jsonl")]
    if sources in ("all", "codex") and codex_dir.is_dir():
        found += [("codex", p) for p in codex_dir.rglob("rollout-*.jsonl")]
    return sorted(found, key=lambda sp: sp[1].stat().st_mtime, reverse=True)


def recent_sessions(limit: int, *, sources: str = "all", skip_ids: set[str] = frozenset(), claude_dir: Path = CLAUDE_DIR,
                    codex_dir: Path = CODEX_DIR, min_prompts: int = 1, min_steps: int = 0) -> list[Session]:
    out: list[Session] = []
    for source, path in find_session_files(claude_dir, codex_dir, sources):
        if len(out) >= limit:
            break
        try:
            s = parse_claude(path) if source == "claude-code" else parse_codex(path)
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't stop the run
            print(f"skip {path}: {e}")
            continue
        if not s or s.session_id in skip_ids or len(s.prompts) < min_prompts or len(s.steps) < min_steps:
            continue
        out.append(s)
    return out
