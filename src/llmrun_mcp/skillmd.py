"""Read and write the SKILL.md format (YAML frontmatter + markdown body).

Also turns loose markdown notes (no frontmatter) into a skill, so old
workflow write-ups can be imported as they are.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.DOTALL)


def parse(text: str, fallback_name: str | None = None) -> dict[str, Any]:
    """Return {name, description, body, tags} from SKILL.md or plain markdown."""
    meta: dict[str, Any] = {}
    body = text
    m = _FRONTMATTER_RE.match(text.lstrip("﻿"))
    if m:
        loaded = yaml.safe_load(m.group(1)) or {}
        if not isinstance(loaded, dict):
            raise ValueError("frontmatter must be a YAML mapping")
        meta, body = loaded, m.group(2)
    body = body.strip()

    name = meta.get("name") or _first_heading(body) or fallback_name
    if not name:
        raise ValueError("could not find a name: add `name:` frontmatter or a # heading")
    description = meta.get("description") or _first_paragraph(body)
    if not description:
        raise ValueError(f"{name}: could not find a description: add `description:` frontmatter")

    tags = meta.get("tags")
    if not tags and isinstance(meta.get("metadata"), dict):
        tags = meta["metadata"].get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return {
        "name": str(name),
        "description": " ".join(str(description).split()),
        "body": body,
        "tags": [str(t) for t in (tags or [])],
    }


def render(skill: dict[str, Any]) -> str:
    """Render a stored skill back to SKILL.md, ready to drop into .claude/skills/<name>/."""
    front = {"name": skill["name"], "description": skill["description"]}
    meta = {"version": skill.get("version", 1), "source": "mcp.llmrun.com"}
    if skill.get("tags"):
        meta["tags"] = skill["tags"]
    front["metadata"] = meta
    return "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + skill["body"].strip() + "\n"


def find_workflow_files(paths: list[Path]) -> list[Path]:
    """SKILL.md files win; other .md files are taken as one workflow each."""
    found: list[Path] = []
    for p in paths:
        if p.is_file():
            found.append(p)
            continue
        skill_dirs = {f.parent for f in p.rglob("SKILL.md")}
        found.extend(sorted(p.rglob("SKILL.md")))
        for f in sorted(p.rglob("*.md")):
            if f.name in ("SKILL.md", "README.md") or any(d in f.parents for d in skill_dirs):
                continue
            found.append(f)
    return found


def fallback_name_for(path: Path) -> str:
    return path.parent.name if path.name == "SKILL.md" else path.stem


def _first_heading(body: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else None


def _first_paragraph(body: str) -> str | None:
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if block and not block.startswith(("#", "```", "-", "*", "|", ">")) and not re.match(r"\d+\.", block):
            return block[:500]
    return None
