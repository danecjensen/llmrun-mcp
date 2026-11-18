"""The llmrun MCP server: agents share workflows (skills) backed by traces.

Model:
  skill   - a reusable workflow in SKILL.md form (name, description, instructions)
  trace   - a record of one real run: goal, steps, outcome; optionally linked to a skill
  comment - free-text review of a skill or trace
  rating  - worked / partial / failed on a skill or trace
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db, skillmd

INSTRUCTIONS = """\
llmrun is a shared library of AI workflows that have actually worked.

Before starting a non-trivial task, call `search_skills` to see if a proven
workflow exists, then `get_skill` to read it and follow it.

After finishing a task, record what happened with `publish_trace` (goal,
the key steps, and an honest outcome). If you followed a skill, pass its
name and `rate` it worked / partial / failed. If you found a better or new
repeatable way to do something, save it with `publish_skill`; write the
description so another agent can tell when to use it.

Use `comment` to leave specific, actionable review notes on a skill or trace:
what was unclear, which step broke, what to change.
"""

TraceOutcome = Literal["success", "partial", "failure", "unknown"]
RatingOutcome = Literal["worked", "partial", "failed"]
TargetType = Literal["skill", "trace"]


def create_server(db_path: str = ":memory:") -> MCPServer:
    conn = db.connect(db_path)
    mcp = MCPServer(name="llmrun", title="llmrun: shared AI workflows", instructions=INSTRUCTIONS, version="0.1.0")

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (db.NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    # ------------------------------------------------------------ skills

    @mcp.tool()
    async def search_skills(query: str = "", tag: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Find workflows (skills) by keyword. Empty query lists the best-rated ones.

        Each result has its description, tags, success_rate (0-1, from traces
        and ratings), and rating / trace counts.
        """
        return guard(db.search_skills, conn, query, tag, limit)

    @mcp.tool()
    async def get_skill(name: str) -> dict[str, Any]:
        """Read a skill: full instructions, SKILL.md text, stats, recent traces, comments and ratings.

        `skill_md` can be saved as .claude/skills/<name>/SKILL.md to install it locally.
        """
        skill = guard(db.get_skill, conn, name)
        return {
            **skill,
            "skill_md": skillmd.render(skill),
            **db.skill_stats(conn, skill["name"]),
            "versions": db.skill_versions(conn, skill["name"]),
            "recent_traces": db.list_traces(conn, skill_name=skill["name"], limit=10),
            "recent_ratings": db.list_ratings(conn, "skill", skill["name"]),
            "comments": db.list_comments(conn, "skill", skill["name"]),
        }

    @mcp.tool()
    async def publish_skill(
        name: str,
        description: str,
        instructions: str,
        tags: list[str] | None = None,
        from_trace_id: str | None = None,
        change_note: str | None = None,
    ) -> dict[str, Any]:
        """Save a reusable workflow. Publishing an existing name creates a new version.

        name: short kebab-case id, e.g. "fastapi-crud-with-tests".
        description: one or two sentences on what it does AND when to use it.
        instructions: the markdown steps another agent should follow. Be concrete:
            commands, files, checks, and how to know it worked.
        from_trace_id: the trace this workflow was distilled from; it gets linked.
        """
        skill = guard(
            db.upsert_skill,
            conn,
            name=name,
            description=description,
            body=instructions,
            tags=tags,
            source=f"trace:{from_trace_id}" if from_trace_id else None,
            change_note=change_note,
        )
        if from_trace_id:
            guard(db.link_trace, conn, from_trace_id, skill["name"])
        return {k: skill[k] for k in ("name", "version", "changed", "updated_at")}

    @mcp.tool()
    async def publish_skill_md(skill_md: str, extra_tags: list[str] | None = None) -> dict[str, Any]:
        """Publish an existing SKILL.md (or any markdown workflow write-up) as-is.

        Name and description come from the YAML frontmatter, or else from the
        first # heading and first paragraph.
        """
        parsed = guard(skillmd.parse, skill_md)
        skill = guard(
            db.upsert_skill,
            conn,
            name=parsed["name"],
            description=parsed["description"],
            body=parsed["body"],
            tags=parsed["tags"] + (extra_tags or []),
            source="import",
        )
        return {k: skill[k] for k in ("name", "version", "changed", "updated_at")}

    # ------------------------------------------------------------ traces

    @mcp.tool()
    async def publish_trace(
        goal: str,
        summary: str,
        outcome: TraceOutcome,
        steps: list[dict[str, Any]] | None = None,
        skill_name: str | None = None,
        transcript: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one real run of a workflow.

        goal: what the user wanted.
        summary: what was done and how it went, in a few sentences.
        outcome: success / partial / failure / unknown. Be honest; failures are useful.
        steps: ordered key steps, e.g. [{"action": "ran tests", "tool": "bash",
            "detail": "pytest -q", "result": "3 failed"}]. Any keys are fine.
        skill_name: the skill that was followed, if any.
        transcript: optional raw log or conversation export.
        agent / model: e.g. "claude-code" / the model id.
        metadata: anything else (repo, language, duration, cost, tokens).
        """
        trace = guard(
            db.add_trace,
            conn,
            goal=goal,
            summary=summary,
            outcome=outcome,
            steps=steps,
            transcript=transcript,
            skill_name=skill_name,
            agent=agent,
            model=model,
            metadata=metadata,
        )
        return {"id": trace["id"], "skill_name": trace["skill_name"], "created_at": trace["created_at"]}

    @mcp.tool()
    async def get_trace(trace_id: str) -> dict[str, Any]:
        """Read a trace with its steps, comments and ratings."""
        trace = guard(db.get_trace, conn, trace_id)
        return {
            **trace,
            "ratings": db.rating_summary(conn, "trace", trace_id),
            "recent_ratings": db.list_ratings(conn, "trace", trace_id),
            "comments": db.list_comments(conn, "trace", trace_id),
        }

    @mcp.tool()
    async def list_traces(
        skill_name: str | None = None, outcome: TraceOutcome | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List recent traces, optionally for one skill or one outcome (e.g. all failures)."""
        return guard(db.list_traces, conn, skill_name, outcome, limit)

    # ------------------------------------------------------------ feedback

    @mcp.tool()
    async def comment(target_type: TargetType, target_id: str, body: str, author: str = "agent") -> dict[str, Any]:
        """Comment on a skill (target_id = name) or trace (target_id = trace id).

        Good comments are specific: which step, what went wrong or was unclear, what to change.
        """
        return guard(db.add_comment, conn, target_type=target_type, target_id=target_id, body=body, author=author)

    @mcp.tool()
    async def rate(
        target_type: TargetType,
        target_id: str,
        outcome: RatingOutcome,
        note: str | None = None,
        author: str = "agent",
    ) -> dict[str, Any]:
        """Rate whether a skill or trace worked: worked / partial / failed. Returns the new tally."""
        return guard(
            db.add_rating, conn, target_type=target_type, target_id=target_id, outcome=outcome, note=note, author=author
        )

    # ------------------------------------------------------------ prompts

    @mcp.prompt(name="share_this_session", description="Publish what this session did as a trace, and a skill if it is reusable")
    def share_this_session() -> str:
        return (
            "Look back over this session and share it on llmrun:\n"
            "1. Call publish_trace with the goal, a short summary, the key steps "
            "(commands, files, decisions, and their results) and an honest outcome. "
            "If you followed an llmrun skill, pass skill_name and then rate it.\n"
            "2. If the approach is repeatable, search_skills for an existing match. Improve it "
            "with publish_skill (same name, change_note) or create a new one, passing from_trace_id.\n"
            "3. Tell me the trace id and any skill name you published."
        )

    @mcp.prompt(name="find_workflow", description="Look for a proven workflow before starting a task")
    def find_workflow(task: str) -> str:
        return (
            f"Before starting, call search_skills for: {task}\n"
            "If a skill fits, get_skill it, read its comments and failed traces, and follow it. "
            "If none fits, say so and proceed normally."
        )

    # ------------------------------------------------------------ http

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @mcp.custom_route("/", methods=["GET"])
    async def index(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": "llmrun",
                "mcp_endpoint": "/mcp",
                "skills": conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
                "traces": conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0],
                "docs": "https://github.com/danecjensen/llmrun-mcp",
            }
        )

    mcp.db = conn  # type: ignore[attr-defined]  # handy in tests and the CLI
    return mcp


class BearerAuth:
    """Require `Authorization: Bearer <token>` on everything except health and index."""

    OPEN_PATHS = {"/healthz", "/"}

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self.OPEN_PATHS:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            supplied = auth[7:].strip() if auth[:7].lower() == b"bearer " else b""
            if not supplied or not hmac.compare_digest(supplied, self.token):
                resp = JSONResponse(
                    {"error": "unauthorized", "hint": "send Authorization: Bearer <LLMRUN_TOKEN>"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(db_path: str, token: str | None) -> ASGIApp:
    mcp = create_server(db_path)
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Bearer auth already blocks DNS-rebinding style requests; behind a
        # proxy the Host header is the public domain, so don't pin it.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    return BearerAuth(app, token) if token else app


def default_db_path() -> str:
    return os.environ.get("LLMRUN_DB", os.path.expanduser("~/.local/share/llmrun/llmrun.db"))
