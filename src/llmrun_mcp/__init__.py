"""llmrun-mcp command line.

  llmrun-mcp serve                  run the HTTP MCP server
  llmrun-mcp import PATH...         publish old workflows (SKILL.md / .md files) to a server
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

    sub.add_parser("new-token", help="print a random bearer token")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        _serve(args)
    elif args.cmd == "import":
        sys.exit(asyncio.run(_import(args)))
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

    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    transport = streamable_http_client(args.url, http_client=create_mcp_http_client(headers=headers))
    async with Client(transport) as client:
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
