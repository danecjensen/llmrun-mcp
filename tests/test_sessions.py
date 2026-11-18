import json
import os
from pathlib import Path

import pytest
from mcp import Client

from llmrun_mcp import sessions
from llmrun_mcp.server import create_server

pytestmark = pytest.mark.anyio

FAKE_KEY = "sk-ant-api03-" + "a" * 40


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _w(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def write_fixtures(root: Path) -> tuple[Path, Path]:
    """A small Claude Code session and a small Codex rollout, in the real on-disk layouts."""
    claude_dir, codex_dir = root / "claude", root / "codex"
    base = {"sessionId": "cc-1", "cwd": "/Users/dane/code/webapp", "gitBranch": "main", "isSidechain": False}
    _w(claude_dir / "-Users-dane-code-webapp" / "cc-1.jsonl", [
        {"type": "summary", "summary": "Fix login"},
        {**base, "type": "user", "isMeta": True, "timestamp": "2026-11-18T10:00:00Z",
         "message": {"role": "user", "content": "<local-command-stdout>noise</local-command-stdout>"}},
        {**base, "type": "user", "timestamp": "2026-11-18T10:00:01Z",
         "message": {"role": "user", "content": f"Fix the login bug. My key is {FAKE_KEY}"}},
        {**base, "type": "assistant", "timestamp": "2026-11-18T10:00:05Z", "message": {
            "role": "assistant", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{"type": "thinking", "thinking": "hmm"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {**base, "type": "user", "timestamp": "2026-11-18T10:00:09Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "1 failed", "is_error": True}]}},
        {**base, "type": "assistant", "isSidechain": True, "timestamp": "2026-11-18T10:00:10Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent chatter"}]}},
        {**base, "type": "assistant", "timestamp": "2026-11-18T10:03:00Z", "message": {
            "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "Fixed it; tests pass."}]}},
    ])
    # Subagent logs live one level deeper and must be ignored.
    _w(claude_dir / "-Users-dane-code-webapp" / "cc-1" / "subagents" / "agent-x.jsonl", [
        {**base, "type": "user", "message": {"role": "user", "content": "sub task"}}])
    os.utime(claude_dir / "-Users-dane-code-webapp" / "cc-1.jsonl", (1, 1))

    _w(codex_dir / "2026" / "11" / "18" / "rollout-2026-11-18T11-00-00-cx-1.jsonl", [
        {"timestamp": "2026-11-18T11:00:00Z", "type": "session_meta",
         "payload": {"id": "cx-1", "cwd": "/Users/dane/code/api", "git": {"branch": "feat"}}},
        {"timestamp": "2026-11-18T11:00:00Z", "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"timestamp": "2026-11-18T11:00:01Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
        {"timestamp": "2026-11-18T11:00:02Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add a /health route"}]}},
        {"timestamp": "2026-11-18T11:00:03Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "call_id": "c1",
            "arguments": json.dumps({"command": ["bash", "-lc", "export GITHUB_TOKEN=ghp_" + "b" * 36 + " && make test"]})}},
        {"timestamp": "2026-11-18T11:00:04Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0}})}},
        {"timestamp": "2026-11-18T11:00:05Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"total_tokens": 1234}}}},
        {"timestamp": "2026-11-18T11:00:06Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Added /health."}]}},
    ])
    return claude_dir, codex_dir


def test_parse_and_order(tmp_path: Path):
    claude_dir, codex_dir = write_fixtures(tmp_path)
    found = sessions.recent_sessions(5, claude_dir=claude_dir, codex_dir=codex_dir)
    assert [s.source for s in found] == ["codex", "claude-code"]  # newest first; subagent log ignored
    cx, cc = found

    assert cc.prompts == [f"Fix the login bug. My key is {FAKE_KEY}"]
    assert cc.model == "claude-opus-5-5" and cc.git_branch == "main"
    assert cc.steps == [{"action": "tool call", "tool": "Bash", "detail": "pytest -q", "result": "ERROR: 1 failed"}]
    assert cc.last_reply == "Fixed it; tests pass."
    assert "subagent chatter" not in "\n".join(cc.transcript)

    assert cx.session_id == "cx-1" and cx.model == "gpt-5-codex" and cx.tokens == 1234
    assert cx.prompts == ["Add a /health route"]
    assert cx.steps[0]["tool"] == "shell" and cx.steps[0]["result"] == "ok"

    assert sessions.recent_sessions(5, claude_dir=claude_dir, codex_dir=codex_dir, skip_ids={"cx-1"})[0].source == "claude-code"
    assert len(sessions.recent_sessions(1, claude_dir=claude_dir, codex_dir=codex_dir)) == 1


def test_scrub():
    scrub = sessions.make_scrubber(extra_secrets=["my-llmrun-token-123"], home="/Users/dane")
    text = "\n".join([
        f"key {FAKE_KEY}",
        "OPENAI_API_KEY=sk-proj-" + "c" * 30,
        "aws AKIAABCDEFGHIJKLMNOP",
        "Authorization: Bearer abcdefghijklmnop.qrs",
        'DB_PASSWORD="hunter2hunter2"',
        "postgres://admin:s3cretpass@db:5432/x",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----",
        "token my-llmrun-token-123",
        "mail dane@example.com",
        "cd /Users/dane/code/webapp",
        "max_tokens=4096 password=$DB_PASS",
    ])
    out = scrub(text)
    for leaked in (FAKE_KEY, "sk-proj-", "AKIAABCDEFGHIJKLMNOP", "abcdefghijklmnop", "hunter2", "s3cretpass", "AAAA",
                   "my-llmrun-token-123", "dane@example.com", "/Users/dane"):
        assert leaked not in out, (leaked, out)
    assert "cd ~/code/webapp" in out
    assert "max_tokens=4096 password=$DB_PASS" in out  # numbers and $VARS are left alone


async def test_trace_publishes(tmp_path: Path):
    claude_dir, codex_dir = write_fixtures(tmp_path)
    scrub = sessions.make_scrubber(home="/Users/dane")
    server = create_server(":memory:")
    async with Client(server) as c:
        for s in sessions.recent_sessions(5, claude_dir=claude_dir, codex_dir=codex_dir):
            trace = sessions.scrub_obj(s.to_trace(), scrub)
            assert "sk-ant-" not in json.dumps(trace) and "ghp_" not in json.dumps(trace)
            res = await c.call_tool("publish_trace", trace)
            assert not res.is_error, res.content
            tid = (res.structured_content.get("result") or res.structured_content)["id"]
            got = (await c.call_tool("get_trace", {"trace_id": tid})).structured_content
            got = got.get("result", got)
            assert got["outcome"] == "unknown" and got["agent"] == s.source
            assert got["metadata"]["cwd"].startswith("~/code/")
