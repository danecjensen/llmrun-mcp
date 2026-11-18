import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client

from llmrun_mcp import skillmd
from llmrun_mcp.server import create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def data(res):
    assert not res.is_error, res.content
    sc = res.structured_content
    return sc.get("result", sc) if isinstance(sc, dict) else sc


async def test_full_loop():
    server = create_server(":memory:")
    async with Client(server) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert {"search_skills", "get_skill", "publish_skill", "publish_skill_md", "publish_trace",
                "get_trace", "list_traces", "comment", "rate"} <= tools

        t = data(await c.call_tool("publish_trace", {
            "goal": "Add a FastAPI endpoint with tests",
            "summary": "Wrote route, wrote pytest, all green",
            "outcome": "success",
            "steps": [{"action": "write test", "tool": "edit"}, {"action": "run", "detail": "pytest -q", "result": "ok"}],
            "agent": "claude-code",
        }))
        assert t["id"].startswith("tr_")

        s = data(await c.call_tool("publish_skill", {
            "name": "FastAPI Endpoint With Tests",
            "description": "Add a FastAPI route test-first. Use when adding an HTTP endpoint to a FastAPI app.",
            "instructions": "1. Write a failing pytest\n2. Add the route\n3. Run pytest -q",
            "tags": ["python", "FastAPI"],
            "from_trace_id": t["id"],
        }))
        assert s == {**s, "name": "fastapi-endpoint-with-tests", "version": 1, "changed": True}

        # same content -> no new version; changed -> v2
        assert data(await c.call_tool("publish_skill", {
            "name": "fastapi-endpoint-with-tests",
            "description": "Add a FastAPI route test-first. Use when adding an HTTP endpoint to a FastAPI app.",
            "instructions": "1. Write a failing pytest\n2. Add the route\n3. Run pytest -q",
            "tags": ["fastapi", "python"]}))["changed"] is False
        v2 = data(await c.call_tool("publish_skill", {
            "name": "fastapi-endpoint-with-tests",
            "description": "Add a FastAPI route test-first. Use when adding an HTTP endpoint to a FastAPI app.",
            "instructions": "1. Write a failing pytest\n2. Add the route\n3. Run pytest -q\n4. Run ruff",
            "change_note": "add lint step"}))
        assert v2["version"] == 2

        await c.call_tool("rate", {"target_type": "skill", "target_id": "fastapi-endpoint-with-tests", "outcome": "worked"})
        await c.call_tool("rate", {"target_type": "trace", "target_id": t["id"], "outcome": "partial", "note": "slow"})
        await c.call_tool("comment", {"target_type": "skill", "target_id": "fastapi-endpoint-with-tests",
                                      "body": "Step 2 should mention dependency injection", "author": "reviewer"})

        hits = data(await c.call_tool("search_skills", {"query": "fastapi endpoint"}))
        assert hits[0]["name"] == "fastapi-endpoint-with-tests"
        assert hits[0]["success_rate"] == 1.0
        assert hits[0]["traces"]["success"] == 1
        assert data(await c.call_tool("search_skills", {"query": "kubernetes"})) == []
        assert data(await c.call_tool("search_skills", {"query": "", "tag": "python"}))[0]["name"] == "fastapi-endpoint-with-tests"

        full = data(await c.call_tool("get_skill", {"name": "fastapi-endpoint-with-tests"}))
        assert full["version"] == 2 and len(full["versions"]) == 2
        assert full["comments"][0]["author"] == "reviewer"
        assert full["recent_traces"][0]["id"] == t["id"]
        reparsed = skillmd.parse(full["skill_md"])
        assert reparsed["name"] == "fastapi-endpoint-with-tests" and "Run ruff" in reparsed["body"]

        tr = data(await c.call_tool("get_trace", {"trace_id": t["id"]}))
        assert tr["skill_name"] == "fastapi-endpoint-with-tests" and tr["ratings"]["partial"] == 1
        assert tr["steps"][1]["detail"] == "pytest -q"


async def test_errors_are_tool_errors():
    async with Client(create_server(":memory:")) as c:
        r = await c.call_tool("get_skill", {"name": "nope"})
        assert r.is_error and "no skill" in r.content[0].text
        r = await c.call_tool("rate", {"target_type": "trace", "target_id": "tr_x", "outcome": "worked"})
        assert r.is_error
        r = await c.call_tool("publish_trace", {"goal": "x", "summary": "y", "outcome": "success", "skill_name": "missing"})
        assert r.is_error
        r = await c.call_tool("search_skills", {"query": 'AND OR "( NEAR'})
        assert not r.is_error


async def test_publish_skill_md_plain_markdown():
    md = "# Release a Python package\n\nCut a release to PyPI with uv. Use when shipping a new version.\n\n1. uv build\n2. uv publish\n"
    async with Client(create_server(":memory:")) as c:
        out = data(await c.call_tool("publish_skill_md", {"skill_md": md, "extra_tags": ["release"]}))
        assert out["name"] == "release-a-python-package"
        sk = data(await c.call_tool("get_skill", {"name": out["name"]}))
        assert sk["description"].startswith("Cut a release") and sk["tags"] == ["release"]


def test_find_workflow_files(tmp_path: Path):
    (tmp_path / "skills/deploy").mkdir(parents=True)
    (tmp_path / "skills/deploy/SKILL.md").write_text("---\nname: deploy\ndescription: Deploy it\n---\nsteps")
    (tmp_path / "skills/deploy/reference.md").write_text("helper doc, not a workflow")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/bugfix.md").write_text("Fix bugs test-first.\n\n1. repro")
    (tmp_path / "README.md").write_text("ignore")
    found = [p.relative_to(tmp_path).as_posix() for p in skillmd.find_workflow_files([tmp_path])]
    assert found == ["skills/deploy/SKILL.md", "notes/bugfix.md"]
    assert skillmd.parse((tmp_path / "notes/bugfix.md").read_text(), "bugfix")["name"] == "bugfix"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_auth_and_import(tmp_path: Path):
    """Run the real server over HTTP, check auth, then use the import CLI against it."""
    import httpx

    port = _free_port()
    env = {"LLMRUN_TOKEN": "secret-token", "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "llmrun_mcp", "serve", "--host", "127.0.0.1", "--port", str(port),
         "--db", str(tmp_path / "t.db")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(base + "/healthz").text == "ok":
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        hdrs = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert httpx.post(base + "/mcp", json=init, headers=hdrs).status_code == 401
        assert httpx.post(base + "/mcp", json=init, headers={**hdrs, "Authorization": "Bearer wrong"}).status_code == 401
        ok = httpx.post(base + "/mcp", json=init, headers={**hdrs, "Authorization": "Bearer secret-token",
                                                            "Host": "mcp.llmrun.com"})
        assert ok.status_code == 200, ok.text

        (tmp_path / "wf").mkdir()
        (tmp_path / "wf/triage.md").write_text("# Triage a flaky test\n\nFind and fix flaky tests. Use when CI is red intermittently.\n\n1. rerun 20x")
        cmd = [sys.executable, "-m", "llmrun_mcp", "import", str(tmp_path / "wf"), "--url", base + "/mcp",
               "--token", "secret-token", "--tag", "legacy"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert "new       triage-a-flaky-test v1" in out.stdout
        again = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert "unchanged triage-a-flaky-test v1" in again.stdout

        from test_sessions import write_fixtures
        claude_dir, codex_dir = write_fixtures(tmp_path)
        cmd = [sys.executable, "-m", "llmrun_mcp", "publish-sessions", "--url", base + "/mcp", "--token", "secret-token",
               "--claude-dir", str(claude_dir), "--codex-dir", str(codex_dir), "--state", str(tmp_path / "state.json")]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.count("published tr_") == 2, out.stdout
        again = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert "no new sessions found" in again.stdout
        traces = httpx.get(base + "/").json()["traces"]
        assert traces == 2
    finally:
        proc.terminate()
        proc.wait(5)
