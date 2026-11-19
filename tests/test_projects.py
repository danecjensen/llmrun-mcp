from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client

from llmrun_mcp import db, projects
from llmrun_mcp.server import create_server
from llmrun_mcp.sessions import normalize_repo

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def data(res):
    assert not res.is_error, res.content
    sc = res.structured_content
    return sc.get("result", sc) if isinstance(sc, dict) else sc


def ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def add(conn, sid, days, goal, cwd=None, repo=None, prompts=()):
    meta = {"session_id": sid, "source": "claude-code", "ended_at": ago(days), "prompts": [goal, *prompts]}
    if cwd:
        meta["cwd"] = cwd
    if repo:
        meta["repo"] = repo
    return db.add_trace(conn, goal=goal, summary="s", outcome="unknown", agent="claude-code", metadata=meta)


def tid(conn, sid):
    return conn.execute("SELECT id FROM traces WHERE json_extract(metadata, '$.session_id') = ?", (sid,)).fetchone()[0]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    # One repo, seen in two checkouts and a worktree; one session from a folder that's gone (no remote recorded).
    add(c, "a1", 2, "Add bearer auth to the llmrun MCP server", "~/code/llmrun-mcp", "github.com/dane/llmrun-mcp")
    add(c, "a2", 5, "Publish sessions as traces in llmrun", "~/code/llmrun-mcp/.claude/worktrees/x", "github.com/dane/llmrun-mcp")
    add(c, "a3", 40, "Old llmrun experiment", "~/code/llmrun-mcp")
    # A plain folder project, long idle.
    add(c, "b1", 200, "Render the wedding video timeline with ffmpeg", "~/video/wedding")
    # Home-folder sessions: two about recipes, one about taxes.
    add(c, "h1", 10, "Plan a sourdough recipe schedule", "~", prompts=["sourdough starter hydration"])
    add(c, "h2", 8, "Sourdough starter feeding recipe", "~", prompts=["recipe hydration sourdough"])
    add(c, "h3", 3, "Estimate quarterly taxes for freelance income", "~")
    return c


def test_grouping(conn):
    groups, key_of = projects.build(conn)
    repo = groups["github.com/dane/llmrun-mcp"]
    assert repo["sessions"] == 3 and repo["kind"] == "repo" and repo["name"] == "llmrun-mcp"
    assert repo["suggested_status"] == "active" and repo["days_idle"] == 2
    assert groups["~/video/wedding"]["suggested_status"] == "stale"
    topics = [g for g in groups.values() if g["kind"] == "topic"]
    assert sorted(g["sessions"] for g in topics) == [1, 2]
    sour = next(g for g in topics if g["sessions"] == 2)
    assert "sourdough" in sour["top_terms"] and sour["name"].startswith("topic: ")
    assert key_of[tid(conn, "h1")] == key_of[tid(conn, "h2")] == sour["key"] == "topic:" + tid(conn, "h1")


def test_topic_keys_stable_when_new_sessions_arrive(conn):
    before, _ = projects.build(conn)
    add(conn, "h4", 1, "Sourdough recipe troubleshooting, dense crumb", "~")
    after, key_of = projects.build(conn)
    assert set(before) <= set(after)
    assert key_of[tid(conn, "h4")] in before


def test_reimport_updates_in_place(conn):
    t1 = add(conn, "x1", 1, "first version", "~/p")
    t2 = add(conn, "x1", 0, "continued session", "~/p")
    assert t1["id"] == t2["id"] and t2["updated"]
    assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 8
    assert db.search_traces(conn, "continued")[0]["id"] == t1["id"]
    assert not db.search_traces(conn, "version") or db.search_traces(conn, "version")[0]["id"] != t1["id"]


def test_fts_backfill(tmp_path):
    path = tmp_path / "x.db"
    c = db.connect(path)
    add(c, "z1", 1, "quarterly taxes")
    c.execute("DELETE FROM traces_fts")
    c.close()
    assert db.search_traces(db.connect(path), "taxes")


def test_normalize_repo():
    assert normalize_repo("git@github.com:Dane/X.git") == "github.com/dane/x"
    assert normalize_repo("https://tok:pw@github.com/dane/x/") == "github.com/dane/x"
    assert normalize_repo("ssh://git@host:2222/a/b.git") == "host/a/b"


async def test_tools_roundtrip():
    server = create_server(":memory:")
    conn = server.db
    add(conn, "a1", 2, "Add bearer auth to the llmrun MCP server", "~/code/llmrun-mcp", "github.com/dane/llmrun-mcp")
    add(conn, "a3", 30, "Sketch llmrun MCP server auth ideas", "~/old/llmrun")
    add(conn, "b1", 200, "Render the wedding video timeline with ffmpeg", "~/video/wedding")
    async with Client(server) as c:
        out = data(await c.call_tool("list_projects", {}))
        assert out["total_projects"] == 3 and out["by_status"] == {"unsorted": 3}
        assert [p["name"] for p in out["projects"]] == ["llmrun-mcp", "llmrun", "wedding"]
        assert out["projects"][0]["related"][0]["key"] == "~/old/llmrun"

        hits = data(await c.call_tool("search_sessions", {"query": "ffmpeg video"}))
        assert hits[0]["project"] == "wedding" and "[ffmpeg]" in hits[0]["snippet"]
        assert data(await c.call_tool("search_sessions", {"query": "ffmpeg", "project": "llmrun-mcp"})) == []

        merged = data(await c.call_tool("label_project", {"project": "~/old/llmrun", "merge_into": "llmrun-mcp"}))
        assert merged["sessions"] == 2
        p = data(await c.call_tool("label_project", {"project": "llmrun-mcp", "status": "active", "name": "llmrun.com"}))
        assert p["name"] == "llmrun.com" and p["status"] == "active"
        data(await c.call_tool("label_project", {"project": "wedding", "status": "archived", "note": "done"}))

        out = data(await c.call_tool("list_projects", {}))
        assert [p["name"] for p in out["projects"]] == ["llmrun.com"]
        assert data(await c.call_tool("list_projects", {"status": "archived"}))["projects"][0]["note"] == "done"

        detail = data(await c.call_tool("get_project", {"project": "llmrun.com"}))
        assert detail["merged_from"] == ["~/old/llmrun"] and len(detail["recent_sessions"]) == 2

        bad = await c.call_tool("get_project", {"project": "nope"})
        assert bad.is_error
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert "organize_projects" in prompts
