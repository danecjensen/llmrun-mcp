"""SQLite storage for skills, traces, comments and ratings.

One file, no ORM. Every public function takes an open connection so the
server and tests can share the same code.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    version     INTEGER NOT NULL DEFAULT 1,
    source      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
    name        TEXT NOT NULL REFERENCES skills(name) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    description TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT NOT NULL,
    change_note TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, body, tags, tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS traces (
    id          TEXT PRIMARY KEY,
    skill_name  TEXT,
    goal        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    steps       TEXT NOT NULL DEFAULT '[]',
    transcript  TEXT,
    agent       TEXT,
    model       TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_skill ON traces(skill_name, created_at);
CREATE INDEX IF NOT EXISTS traces_session ON traces(json_extract(metadata, '$.session_id'));

CREATE VIRTUAL TABLE IF NOT EXISTS traces_fts USING fts5(
    id UNINDEXED, goal, summary, prompts, transcript, tokenize = 'porter unicode61'
);

-- Decisions about projects found in session traces (see projects.py).
CREATE TABLE IF NOT EXISTS project_labels (
    key         TEXT PRIMARY KEY,
    status      TEXT,
    name        TEXT,
    note        TEXT,
    merge_into  TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_target ON comments(target_type, target_id);

CREATE TABLE IF NOT EXISTS ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    note        TEXT,
    author      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ratings_target ON ratings(target_type, target_id);
"""

TRACE_OUTCOMES = ("success", "partial", "failure", "unknown")
RATING_OUTCOMES = ("worked", "partial", "failed")
TARGET_TYPES = ("skill", "trace")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class NotFound(LookupError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("name must contain at least one letter or digit")
    return slug[:64]


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    n_traces = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    if conn.execute("SELECT COUNT(*) FROM traces_fts").fetchone()[0] != n_traces:
        # First start after upgrading, or the index drifted: rebuild it.
        with conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM traces_fts")
            for row in conn.execute("SELECT * FROM traces").fetchall():
                _index_trace(conn, _trace_row(row))
    return conn


# ---------------------------------------------------------------- skills


def _skill_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    return d


def _index_skill(conn: sqlite3.Connection, s: dict[str, Any]) -> None:
    conn.execute("DELETE FROM skills_fts WHERE name = ?", (s["name"],))
    conn.execute(
        "INSERT INTO skills_fts(name, description, body, tags) VALUES (?, ?, ?, ?)",
        (s["name"], s["description"], s["body"], " ".join(s["tags"])),
    )


def upsert_skill(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    body: str,
    tags: list[str] | None = None,
    source: str | None = None,
    change_note: str | None = None,
) -> dict[str, Any]:
    """Create a skill, or save a new version if the name exists and content changed."""
    name = slugify(name)
    description = description.strip()
    body = body.strip()
    if not description:
        raise ValueError("description is required: say what the workflow does and when to use it")
    if not body:
        raise ValueError("instructions/body is required")
    ts = now()
    with conn:
        conn.execute("BEGIN")
        existing = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
        if tags is None and existing is not None:
            tags = json.loads(existing["tags"])  # new version without tags keeps the old ones
        tags = sorted({t.strip().lower() for t in (tags or []) if t.strip()})
        if existing is None:
            version = 1
            conn.execute(
                "INSERT INTO skills(name, description, body, tags, version, source, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (name, description, body, json.dumps(tags), source, ts, ts),
            )
        else:
            same = (
                existing["description"] == description
                and existing["body"] == body
                and json.loads(existing["tags"]) == tags
            )
            if same:
                conn.execute("COMMIT")
                return {**_skill_row(existing), "changed": False}
            version = existing["version"] + 1
            conn.execute(
                "UPDATE skills SET description=?, body=?, tags=?, version=?, updated_at=?,"
                " source=COALESCE(?, source) WHERE name=?",
                (description, body, json.dumps(tags), version, ts, source, name),
            )
        conn.execute(
            "INSERT INTO skill_versions(name, version, description, body, tags, change_note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version, description, body, json.dumps(tags), change_note, ts),
        )
        skill = get_skill(conn, name)
        _index_skill(conn, skill)
    return {**skill, "changed": True}


def get_skill(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM skills WHERE name = ?", (slugify(name),)).fetchone()
    if row is None:
        raise NotFound(f"no skill named {name!r}")
    return _skill_row(row)


def skill_versions(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT version, change_note, created_at FROM skill_versions WHERE name = ? ORDER BY version DESC",
        (slugify(name),),
    ).fetchall()
    return [dict(r) for r in rows]


def _fts_query(text: str) -> str:
    # Quote every word so user text can't break FTS5 syntax; OR them for recall.
    words = re.findall(r"\w+", text.lower())
    return " OR ".join(f'"{w}"' for w in words)


def search_skills(
    conn: sqlite3.Connection, query: str = "", tag: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    fts = _fts_query(query) if query else ""
    if fts:
        rows = conn.execute(
            "SELECT s.* FROM skills_fts f JOIN skills s ON s.name = f.name"
            " WHERE skills_fts MATCH ? ORDER BY bm25(skills_fts, 10.0, 5.0, 1.0, 3.0) LIMIT ?",
            (fts, limit * 3),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM skills ORDER BY updated_at DESC LIMIT ?", (limit * 3,)).fetchall()
    out = []
    for row in rows:
        s = _skill_row(row)
        if tag and tag.lower() not in s["tags"]:
            continue
        stats = skill_stats(conn, s["name"])
        out.append(
            {
                "name": s["name"],
                "description": s["description"],
                "tags": s["tags"],
                "version": s["version"],
                "updated_at": s["updated_at"],
                **stats,
            }
        )
        if len(out) >= limit:
            break
    if not query:
        # No relevance signal, so float the most proven workflows to the top.
        out.sort(key=lambda s: (s["success_rate"] or 0, s["ratings"]["total"]), reverse=True)
    return out


def skill_stats(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    ratings = rating_summary(conn, "skill", name)
    trace_rows = conn.execute(
        "SELECT outcome, COUNT(*) n FROM traces WHERE skill_name = ? GROUP BY outcome", (name,)
    ).fetchall()
    traces = {o: 0 for o in TRACE_OUTCOMES} | {r["outcome"]: r["n"] for r in trace_rows}
    traces["total"] = sum(r["n"] for r in trace_rows)
    # Success rate blends trace outcomes and explicit ratings; partial counts half.
    good = traces["success"] + ratings["worked"] + 0.5 * (traces["partial"] + ratings["partial"])
    judged = traces["success"] + traces["partial"] + traces["failure"] + ratings["total"]
    return {
        "ratings": ratings,
        "traces": traces,
        "success_rate": round(good / judged, 2) if judged else None,
    }


# ---------------------------------------------------------------- traces


def add_trace(
    conn: sqlite3.Connection,
    *,
    goal: str,
    summary: str,
    outcome: str,
    steps: list[Any] | None = None,
    transcript: str | None = None,
    skill_name: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if outcome not in TRACE_OUTCOMES:
        raise ValueError(f"outcome must be one of {TRACE_OUTCOMES}")
    if not goal.strip() or not summary.strip():
        raise ValueError("goal and summary are required")
    if skill_name:
        skill_name = get_skill(conn, skill_name)["name"]
    values = (
        skill_name,
        goal.strip(),
        summary.strip(),
        outcome,
        json.dumps(steps or []),
        transcript,
        agent,
        model,
        json.dumps(metadata or {}),
    )
    session_id = (metadata or {}).get("session_id")
    existing = None
    if session_id:
        # Re-publishing an imported session (it continued, or the importer
        # improved) updates the same trace, keeping its comments and ratings.
        existing = conn.execute(
            "SELECT id, skill_name FROM traces WHERE json_extract(metadata, '$.session_id') = ?", (session_id,)
        ).fetchone()
    with conn:
        conn.execute("BEGIN")
        if existing:
            trace_id = existing["id"]
            conn.execute(
                "UPDATE traces SET skill_name=COALESCE(?, skill_name), goal=?, summary=?, outcome=?, steps=?,"
                " transcript=?, agent=?, model=?, metadata=? WHERE id=?",
                (*values, trace_id),
            )
        else:
            trace_id = "tr_" + uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO traces(skill_name, goal, summary, outcome, steps, transcript, agent, model, metadata,"
                " created_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, now(), trace_id),
            )
        trace = get_trace(conn, trace_id)
        _index_trace(conn, trace)
    return {**trace, "updated": existing is not None}


def _index_trace(conn: sqlite3.Connection, t: dict[str, Any]) -> None:
    prompts = t["metadata"].get("prompts") or []
    conn.execute("DELETE FROM traces_fts WHERE id = ?", (t["id"],))
    conn.execute(
        "INSERT INTO traces_fts(id, goal, summary, prompts, transcript) VALUES (?, ?, ?, ?, ?)",
        (t["id"], t["goal"], t["summary"], "\n".join(map(str, prompts)), t["transcript"] or ""),
    )


def search_traces(
    conn: sqlite3.Connection, query: str, ids: set[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Full-text search over traces; best matches first, with a highlighted snippet."""
    fts = _fts_query(query)
    if not fts:
        raise ValueError("query needs at least one word")
    rows = conn.execute(
        "SELECT t.id, t.goal, t.agent, t.created_at, t.metadata,"
        " snippet(traces_fts, -1, '[', ']', ' … ', 24) AS snippet"
        " FROM traces_fts f JOIN traces t ON t.id = f.id WHERE traces_fts MATCH ?"
        " ORDER BY bm25(traces_fts, 0, 6.0, 2.0, 4.0, 1.0) LIMIT ?",
        (fts, 2000 if ids is not None else max(1, min(limit, 100))),
    ).fetchall()
    out = []
    for r in rows:
        if ids is not None and r["id"] not in ids:
            continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def _trace_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["steps"] = json.loads(d["steps"])
    d["metadata"] = json.loads(d["metadata"])
    return d


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
    if row is None:
        raise NotFound(f"no trace with id {trace_id!r}")
    return _trace_row(row)


def link_trace(conn: sqlite3.Connection, trace_id: str, skill_name: str) -> None:
    get_trace(conn, trace_id)
    conn.execute("UPDATE traces SET skill_name = ? WHERE id = ?", (get_skill(conn, skill_name)["name"], trace_id))


def list_traces(
    conn: sqlite3.Connection,
    skill_name: str | None = None,
    outcome: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    sql = "SELECT id, skill_name, goal, summary, outcome, agent, model, created_at FROM traces WHERE 1=1"
    args: list[Any] = []
    if skill_name:
        sql += " AND skill_name = ?"
        args.append(slugify(skill_name))
    if outcome:
        sql += " AND outcome = ?"
        args.append(outcome)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(max(1, min(limit, 100)))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- feedback


def _check_target(conn: sqlite3.Connection, target_type: str, target_id: str) -> str:
    if target_type == "skill":
        return get_skill(conn, target_id)["name"]
    if target_type == "trace":
        return get_trace(conn, target_id)["id"]
    raise ValueError(f"target_type must be one of {TARGET_TYPES}")


def add_comment(
    conn: sqlite3.Connection, *, target_type: str, target_id: str, body: str, author: str
) -> dict[str, Any]:
    target_id = _check_target(conn, target_type, target_id)
    if not body.strip():
        raise ValueError("comment body is empty")
    cur = conn.execute(
        "INSERT INTO comments(target_type, target_id, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (target_type, target_id, author, body.strip(), now()),
    )
    return dict(conn.execute("SELECT * FROM comments WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_comments(conn: sqlite3.Connection, target_type: str, target_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, author, body, created_at FROM comments WHERE target_type = ? AND target_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (target_type, target_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def add_rating(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    outcome: str,
    note: str | None,
    author: str,
) -> dict[str, Any]:
    if outcome not in RATING_OUTCOMES:
        raise ValueError(f"outcome must be one of {RATING_OUTCOMES}")
    target_id = _check_target(conn, target_type, target_id)
    conn.execute(
        "INSERT INTO ratings(target_type, target_id, outcome, note, author, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (target_type, target_id, outcome, note, author, now()),
    )
    return rating_summary(conn, target_type, target_id)


def rating_summary(conn: sqlite3.Connection, target_type: str, target_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT outcome, COUNT(*) n FROM ratings WHERE target_type = ? AND target_id = ? GROUP BY outcome",
        (target_type, target_id),
    ).fetchall()
    out = {o: 0 for o in RATING_OUTCOMES} | {r["outcome"]: r["n"] for r in rows}
    out["total"] = sum(r["n"] for r in rows)
    return out


def list_ratings(conn: sqlite3.Connection, target_type: str, target_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT outcome, note, author, created_at FROM ratings WHERE target_type = ? AND target_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (target_type, target_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
