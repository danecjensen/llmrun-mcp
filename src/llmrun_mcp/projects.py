"""Find your projects in session traces, so an agent can help you sort them.

Each trace imported by `publish-sessions` carries the session's folder, git
remote and user prompts. Sessions are grouped:

  1. by git repo (all checkouts and worktrees of one repo are one project),
  2. else by folder,
  3. sessions started in a catch-all folder (~, Desktop, /tmp) are clustered
     by topic: TF-IDF over their prompts, assigned greedily in time order so
     adding new sessions never moves old ones.

Your decisions (status, display name, notes, "this is the same as that")
live in the project_labels table, keyed by the group key, and are applied on
every read. Nothing here is precomputed; it is cheap enough for one person's
few thousand sessions.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from .db import NotFound, now

STATUSES = ("active", "paused", "stale", "archived")
ACTIVE_DAYS = 21  # suggested status: active if touched in the last 3 weeks,
PAUSED_DAYS = 90  # paused within 3 months, else stale
TOPIC_THRESHOLD = 0.2  # cosine similarity needed to join an existing topic cluster
RELATED_THRESHOLD = 0.3

_CATCH_ALL = re.compile(r"^(?:~|/|~/(?:desktop|downloads|documents))/?$|^(?:/private)?(?:/tmp|/var/folders)(?:/|$)", re.I)
_WORKTREE = re.compile(r"/(?:\.claude/worktrees|\.codex/worktrees|\.worktrees|worktrees)/.*$")
_WORD = re.compile(r"[a-z][a-z0-9]*(?:[_\-][a-z0-9]+)*")
STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing done down during each few for from further had has have having he her here hers him his
how i if in into is it its itself just let me more most my myself no nor not now of off on once only or other our out
over own same she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours yourself
please thanks thank okay ok yes yeah sure want need like make use using used get got go going let lets know think
see look looks one two new also still really something thing things way able just right good well try trying
add added update updated change changes fix fixed run running work working works file files code create created
help show tell give take keep put set call called check sure now then again first next last time
claude codex session user assistant tool tools agent request redacted email
""".split())


def _parse_time(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def tokens(text: str) -> list[str]:
    words = _WORD.findall(text.lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]


def _tfidf(docs: list[list[str]]) -> list[dict[str, float]]:
    """Sublinear TF-IDF, L2-normalised. Terms in every doc still keep a little weight."""
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    out = []
    for d in docs:
        v = {t: (1 + math.log(c)) * math.log((n + 1) / (df[t] + 0.5)) for t, c in Counter(d).items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        out.append({t: x / norm for t, x in v.items()})
    return out


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def _add(acc: dict[str, float], v: dict[str, float]) -> None:
    for t, x in v.items():
        acc[t] = acc.get(t, 0.0) + x


def _norm(v: dict[str, float]) -> dict[str, float]:
    n = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {t: x / n for t, x in v.items()}


# ---------------------------------------------------------------- loading


def load_sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every trace, flattened to what grouping needs. Oldest first."""
    out = []
    for r in conn.execute("SELECT id, goal, summary, agent, created_at, metadata FROM traces").fetchall():
        m = json.loads(r["metadata"] or "{}")
        when = _parse_time(m.get("ended_at")) or _parse_time(m.get("started_at")) or _parse_time(r["created_at"])
        prompts = m.get("prompts") or []
        cwd = m.get("cwd")
        out.append(
            {
                "id": r["id"],
                "goal": r["goal"],
                "agent": r["agent"],
                "branch": m.get("git_branch"),
                "repo": m.get("repo"),
                "cwd": _WORKTREE.sub("", cwd) if cwd else None,
                "when": when,
                "tokens": tokens(" ".join([r["goal"], *map(str, prompts)]) if prompts else r["goal"] + " " + r["summary"]),
            }
        )
    out.sort(key=lambda s: (s["when"] or datetime.min.replace(tzinfo=UTC), s["id"]))
    return out


def _labels(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM project_labels").fetchall()}


def _resolve(key: str, labels: dict[str, dict[str, Any]]) -> str:
    seen = {key}
    while (target := (labels.get(key) or {}).get("merge_into")) and target not in seen:
        seen.add(target)
        key = target
    return key


# ---------------------------------------------------------------- grouping


def build(conn: sqlite3.Connection, today: datetime | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """All project groups by key, plus a map of trace id -> group key."""
    today = today or datetime.now(UTC)
    sessions = load_sessions(conn)
    labels = _labels(conn)

    # A folder that ever had a git remote belongs to that repo, even in sessions
    # recorded after the folder was deleted or before the remote was added.
    cwd_repo = {s["cwd"]: s["repo"] for s in sessions if s["cwd"] and s["repo"]}
    loose = []
    key_of: dict[str, str] = {}
    for s in sessions:
        repo = s["repo"] or cwd_repo.get(s["cwd"])
        if repo:
            key_of[s["id"]] = repo
        elif s["cwd"] and not _CATCH_ALL.match(s["cwd"]):
            key_of[s["id"]] = s["cwd"]
        else:
            loose.append(s)

    # Topic clusters for sessions started from a catch-all folder.
    vecs = _tfidf([s["tokens"] for s in sessions])
    vec_of = {s["id"]: v for s, v in zip(sessions, vecs)}
    clusters: list[tuple[str, dict[str, float]]] = []  # (key, summed vector)
    for s in loose:
        v = vec_of[s["id"]]
        if not v:
            key_of[s["id"]] = "topic:misc"
            continue
        best, score = None, TOPIC_THRESHOLD
        for i, (_, acc) in enumerate(clusters):
            c = _cos(v, _norm(acc))
            if c >= score:
                best, score = i, c
        if best is None:
            clusters.append(("topic:" + s["id"], dict(v)))
            key_of[s["id"]] = clusters[-1][0]
        else:
            _add(clusters[best][1], v)
            key_of[s["id"]] = clusters[best][0]

    key_of = {tid: _resolve(k, labels) for tid, k in key_of.items()}

    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        members[key_of[s["id"]]].append(s)

    keys = list(members)
    group_vecs = _tfidf([[t for s in members[k] for t in s["tokens"]] for k in keys])
    groups: dict[str, dict[str, Any]] = {}
    for k, gv in zip(keys, group_vecs):
        ss = members[k]
        label = labels.get(k) or {}
        last = max((s["when"] for s in ss if s["when"]), default=None)
        first = min((s["when"] for s in ss if s["when"]), default=None)
        idle = (today - last).days if last else None
        terms = [t for t, _ in sorted(gv.items(), key=lambda x: -x[1])[:8]]
        repo = next((s["repo"] or cwd_repo.get(s["cwd"]) for s in ss if s["repo"] or cwd_repo.get(s["cwd"])), None)
        paths = Counter(s["cwd"] for s in ss if s["cwd"])
        if k.startswith("topic:"):
            kind = "topic"
            auto_name = "misc (no clear topic)" if k == "topic:misc" else "topic: " + ", ".join(terms[:3])
        else:
            kind = "repo" if repo == k else "folder"
            auto_name = PurePosixPath(k).name or k
        groups[k] = {
            "key": k,
            "name": label.get("name") or auto_name,
            "kind": kind,
            "repo": repo,
            "paths": [p for p, _ in paths.most_common(5)],
            "sessions": len(ss),
            "agents": dict(Counter(s["agent"] or "?" for s in ss)),
            "first_seen": first.date().isoformat() if first else None,
            "last_seen": last.date().isoformat() if last else None,
            "days_idle": idle,
            "active_days": len({s["when"].date() for s in ss if s["when"]}),
            "top_terms": terms,
            "recent_goals": [_clip(s["goal"], 160) for s in reversed(ss[-3:])],
            "status": label.get("status"),
            "suggested_status": None if idle is None else
            "active" if idle <= ACTIVE_DAYS else "paused" if idle <= PAUSED_DAYS else "stale",
            "note": label.get("note"),
            "_vec": gv,
        }

    for g in groups.values():
        related = sorted(
            ((o["key"], round(_cos(g["_vec"], o["_vec"]), 2)) for o in groups.values() if o["key"] != g["key"]),
            key=lambda x: -x[1],
        )
        g["related"] = [{"key": k, "name": groups[k]["name"], "similarity": c} for k, c in related[:3] if c >= RELATED_THRESHOLD]
    for g in groups.values():
        del g["_vec"]
    return groups, key_of


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def find(groups: dict[str, dict[str, Any]], project: str) -> dict[str, Any]:
    """Look a group up by key, or by name / repo / folder name (case-insensitive)."""
    if project in groups:
        return groups[project]
    p = project.strip().lower()
    hits = [
        g for g in groups.values()
        if p in (g["name"].lower(), (g["repo"] or "").lower(), g["key"].lower().rsplit("/", 1)[-1])
    ]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"{project!r} matches several projects; use one of these keys: {[g['key'] for g in hits]}")
    raise NotFound(f"no project {project!r}; call list_projects for keys")


# ---------------------------------------------------------------- tool bodies


def list_projects(
    conn: sqlite3.Connection, status: str | None = None, since_days: int | None = None, limit: int = 50
) -> dict[str, Any]:
    groups, _ = build(conn)
    rows = list(groups.values())
    if status == "all":
        pass
    elif status == "unsorted":
        rows = [g for g in rows if not g["status"]]
    elif status:
        rows = [g for g in rows if g["status"] == status]
    else:
        rows = [g for g in rows if g["status"] != "archived"]
    if since_days is not None:
        rows = [g for g in rows if g["days_idle"] is not None and g["days_idle"] <= since_days]
    rows.sort(key=lambda g: (g["last_seen"] or "", g["sessions"]), reverse=True)
    counts = Counter(g["status"] or "unsorted" for g in groups.values())
    return {
        "total_projects": len(groups),
        "by_status": dict(counts),
        "shown": min(len(rows), limit),
        "projects": rows[: max(1, min(limit, 200))],
    }


def get_project(conn: sqlite3.Connection, project: str, limit: int = 50) -> dict[str, Any]:
    groups, key_of = build(conn)
    g = find(groups, project)
    ids = [tid for tid, k in key_of.items() if k == g["key"]]
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, goal, agent, created_at, metadata FROM traces WHERE id IN ({q})", ids
    ).fetchall() if ids else []
    sessions = []
    for r in rows:
        m = json.loads(r["metadata"] or "{}")
        sessions.append(
            {
                "trace_id": r["id"],
                "when": (m.get("ended_at") or m.get("started_at") or r["created_at"])[:16],
                "agent": r["agent"],
                "branch": m.get("git_branch"),
                "prompts": m.get("user_prompts"),
                "goal": _clip(r["goal"], 240),
            }
        )
    sessions.sort(key=lambda s: s["when"], reverse=True)
    merged_from = [k for k, lab in _labels(conn).items() if lab.get("merge_into") == g["key"]]
    return {**g, "merged_from": merged_from, "recent_sessions": sessions[: max(1, min(limit, 200))]}


def label_project(
    conn: sqlite3.Connection,
    project: str,
    status: str | None = None,
    name: str | None = None,
    note: str | None = None,
    merge_into: str | None = None,
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    groups, _ = build(conn)
    key = find(groups, project)["key"]
    target = None
    if merge_into:
        target = find(groups, merge_into)["key"]
        if target == key:
            raise ValueError("can't merge a project into itself")
    conn.execute(
        "INSERT INTO project_labels(key, status, name, note, merge_into, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET status=COALESCE(excluded.status, status), name=COALESCE(excluded.name, name),"
        " note=COALESCE(excluded.note, note), merge_into=COALESCE(excluded.merge_into, merge_into),"
        " updated_at=excluded.updated_at",
        (key, status, name, note, target, now()),
    )
    groups, _ = build(conn)
    return groups[target or key]


def search_sessions(
    conn: sqlite3.Connection, query: str, project: str | None = None, since_days: int | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    from . import db

    groups, key_of = build(conn)
    ids: set[str] | None = None
    if project:
        k = find(groups, project)["key"]
        ids = {tid for tid, gk in key_of.items() if gk == k}
    hits = db.search_traces(conn, query, ids=ids, limit=200 if since_days is not None else limit)
    today = datetime.now(UTC)
    out = []
    for h in hits:
        m = json.loads(h["metadata"] or "{}")
        when = _parse_time(m.get("ended_at") or m.get("started_at") or h["created_at"])
        if since_days is not None and (when is None or (today - when).days > since_days):
            continue
        g = groups.get(key_of.get(h["id"], ""), {})
        out.append(
            {
                "trace_id": h["id"],
                "when": when.date().isoformat() if when else None,
                "project": g.get("name"),
                "project_key": g.get("key"),
                "agent": h["agent"],
                "goal": _clip(h["goal"], 200),
                "snippet": _clip(h["snippet"], 300),
            }
        )
        if len(out) >= limit:
            break
    return out
