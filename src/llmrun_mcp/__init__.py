"""llmrun-mcp command line.

  llmrun-mcp serve                  run the HTTP MCP server
  llmrun-mcp import PATH...         publish old workflows (SKILL.md / .md files) to a server
  llmrun-mcp publish-sessions       publish your recent Claude Code / Codex sessions as traces
  llmrun-mcp new-token              print a random token for LLMRUN_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="llmrun-mcp")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the MCP server over streamable HTTP")
    s.add_argument("--host", default=os.environ.get("LLMRUN_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("LLMRUN_PORT", "8000")))
    s.add_argument("--db", default=None, help="SQLite path (default $LLMRUN_DB or ~/.local/share/llmrun/llmrun.db)")
    s.add_argument("--no-auth", action="store_true", help="allow running without LLMRUN_TOKEN (local dev only)")

    i = sub.add_parser("import", help="publish SKILL.md / markdown workflow files to a server")
    i.add_argument("paths", nargs="+", type=Path)
    i.add_argument("--url", default=os.environ.get("LLMRUN_URL", "https://mcp.llmrun.com/mcp"))
    i.add_argument("--token", default=os.environ.get("LLMRUN_TOKEN"))
    i.add_argument("--tag", action="append", default=[], help="extra tag for every imported skill (repeatable)")
    i.add_argument("--dry-run", action="store_true", help="parse and list, don't publish")

    ps = sub.add_parser("publish-sessions", help="publish recent local Claude Code / Codex sessions as traces")
    ps.add_argument("--limit", type=int, default=5, help="how many sessions to publish (default 5, newest first)")
    ps.add_argument("--all", action="store_true", help="publish every session, not just the newest --limit")
    ps.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    ps.add_argument("--url", default=os.environ.get("LLMRUN_URL", "https://mcp.llmrun.com/mcp"))
    ps.add_argument("--token", default=os.environ.get("LLMRUN_TOKEN"))
    ps.add_argument("--dry-run", action="store_true", help="show what would be published, don't upload")
    ps.add_argument("--out", type=Path, help="also write each scrubbed trace as JSON here, to inspect before uploading")
    ps.add_argument("--min-steps", type=int, default=1, help="skip sessions with fewer tool calls (default 1)")
    ps.add_argument("--force", action="store_true",
                    help="re-send sessions that were already uploaded (updates them in place, no duplicates)")
    ps.add_argument("--keep-emails", action="store_true", help="don't replace email addresses with [email]")
    ps.add_argument("--claude-dir", type=Path, default=Path("~/.claude/projects").expanduser())
    ps.add_argument("--codex-dir", type=Path, default=Path("~/.codex/sessions").expanduser())
    ps.add_argument("--state", type=Path, default=Path("~/.config/llmrun/published-sessions.json").expanduser(),
                    help="remembers which sessions were uploaded, so re-runs skip them")

    sub.add_parser("new-token", help="print a random bearer token")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        _serve(args)
    elif args.cmd == "import":
        sys.exit(asyncio.run(_import(args)))
    elif args.cmd == "publish-sessions":
        sys.exit(asyncio.run(_publish_sessions(args)))
    elif args.cmd == "new-token":
        print(secrets.token_urlsafe(32))


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app, default_db_path

    token = os.environ.get("LLMRUN_TOKEN")
    if not token and not args.no_auth:
        sys.exit("LLMRUN_TOKEN is not set. Set it, or pass --no-auth for local testing.")
    db_path = args.db or default_db_path()
    print(f"llmrun: db={db_path} auth={'on' if token else 'OFF'} http://{args.host}:{args.port}/mcp", flush=True)
    uvicorn.run(create_app(db_path, token), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")


def _mcp_client(url: str, token: str | None):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(streamable_http_client(url, http_client=create_mcp_http_client(headers=headers)))


async def _publish_sessions(args: argparse.Namespace) -> int:
    import json
    from collections import Counter

    from . import sessions

    if not args.dry_run and not args.token:
        print("LLMRUN_TOKEN is not set (or pass --token). Use --dry-run to preview without one.", file=sys.stderr)
        return 2
    state: dict = {}
    if args.state.exists():
        try:
            state = json.loads(args.state.read_text())
        except json.JSONDecodeError:
            state = {}
    skip = set() if args.force else set(state.get(args.url, {}))

    found = sessions.recent_sessions(
        10**9 if args.all else args.limit, sources=args.source, skip_ids=skip, claude_dir=args.claude_dir,
        codex_dir=args.codex_dir, min_steps=args.min_steps,
    )
    if not found:
        print("no new sessions found" + (" (already-published ones are skipped; --force re-sends them)" if skip else ""))
        return 0

    scrub = sessions.make_scrubber(extra_secrets=[args.token or ""], emails=not args.keep_emails)
    traces = [(s, sessions.scrub_obj(s.to_trace(), scrub)) for s in found]

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    for s, t in traces if len(traces) <= 20 or args.out else []:
        m = t["metadata"]
        print(f"{s.source:11} {m.get('started_at', '?')[:16]}  {m.get('project')}  "
              f"{m['user_prompts']} prompts, {m['tool_calls']} tool calls, transcript {len(t['transcript']):,} chars")
        print(f"            goal: {t['goal'][:120].replace(chr(10), ' ')}")
        if args.out:
            f = args.out / f"{s.source}-{s.session_id}.json"
            f.write_text(json.dumps(t, indent=2, ensure_ascii=False))
            print(f"            wrote {f}")
    if len(traces) > 20 and not args.out:
        projects = Counter(t["metadata"].get("repo") or t["metadata"].get("project") for _, t in traces)
        print(f"{len(traces)} sessions; top projects: " + ", ".join(f"{p} ({n})" for p, n in projects.most_common(15)))
    if args.dry_run:
        print(f"dry run: {len(traces)} trace(s) not uploaded")
        return 0

    failures = 0
    published = state.setdefault(args.url, {})
    async with _mcp_client(args.url, args.token) as client:
        for s, t in traces:
            res = await client.call_tool("publish_trace", t)
            if res.is_error:
                failures += 1
                print(f"FAIL {s.path}: {res.content[0].text if res.content else res}", file=sys.stderr)
                continue
            out = res.structured_content or {}
            out = out.get("result", out)
            published[s.session_id] = out.get("id")
            verb = "updated" if out.get("updated") else "published"
            print(f"{verb} {out.get('id')}  <- {s.source} {s.session_id}")
            if len(published) % 50 == 0:  # a long --all run can be interrupted and resumed
                args.state.parent.mkdir(parents=True, exist_ok=True)
                args.state.write_text(json.dumps(state, indent=2))
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2))
    print(f"done: {len(traces) - failures} published, {failures} failed")
    return 1 if failures else 0


async def _import(args: argparse.Namespace) -> int:
    from . import skillmd

    files = skillmd.find_workflow_files(args.paths)
    if not files:
        print("no .md files found", file=sys.stderr)
        return 1

    parsed = []
    failures = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            meta = skillmd.parse(text, fallback_name=skillmd.fallback_name_for(f))
            # Keep the original frontmatter-less files importable by giving them one.
            if not text.lstrip().startswith("---"):
                text = skillmd.render({**meta, "version": 1})
            parsed.append((f, meta, text))
        except Exception as e:  # noqa: BLE001 - report and keep going
            failures += 1
            print(f"skip {f}: {e}", file=sys.stderr)

    if args.dry_run:
        for f, meta, _ in parsed:
            print(f"{meta['name']:40} {f}\n    {meta['description'][:110]}")
        return 1 if failures else 0

    async with _mcp_client(args.url, args.token) as client:
        for f, meta, text in parsed:
            res = await client.call_tool("publish_skill_md", {"skill_md": text, "extra_tags": args.tag})
            if res.is_error:
                failures += 1
                print(f"FAIL {f}: {res.content[0].text if res.content else res}", file=sys.stderr)
                continue
            out = res.structured_content or {}
            out = out.get("result", out)
            state = "new" if out.get("version") == 1 and out.get("changed") else ("updated" if out.get("changed") else "unchanged")
            print(f"{state:9} {out.get('name')} v{out.get('version')}  <- {f}")
    print(f"done: {len(files)} files, {failures} failed")
    return 1 if failures else 0
