"""Try the guard against your own Gmail with a token from Google's OAuth Playground.

    uv run python scripts/playground_check.py [--domain example.com] [--upstream gmail-mcp]

It asks for the access token without echoing it (or reads GOOGLE_ACCESS_TOKEN), starts the guard
in local test mode (--dev) on 127.0.0.1, and checks, as an MCP client:

- a read-only token (gmail.readonly + userinfo.email, openid optional) is accepted, the agent
  sees exactly the allowed tools, list_labels and search_threads work, and write tools such as
  trash_thread or create_draft are refused without reaching Gmail;
- a token with more access (say gmail.modify or https://mail.google.com/) is refused outright.

The token stays on this machine: it goes only to Google and to the guard on 127.0.0.1. Nothing is
written to disk, and nothing in your mailbox is changed (the guard can't, and the token
shouldn't be able to either). Mail is shown only as subjects of the most recent threads.
"""

import argparse
import asyncio
import getpass
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mcp_guard.config import EMAIL_SCOPE, GuardConfig, Settings, Upstream
from mcp_guard.google import GOOGLE, GoogleEndpoints
from mcp_guard.server import build_app, configure_logging

ROOT = Path(__file__).resolve().parent.parent
# Tools a read-only guard must never let through; Gmail MCP has the first three.
WRITE_TOOLS = ["trash_thread", "create_draft", "label_message", "send_message"]


def _out(line: str = "") -> None:
    print(line, flush=True)


async def _scopes(token: str, endpoints: GoogleEndpoints) -> list[str] | None:
    """What Google says the token can do, or None if Google doesn't accept it."""
    async with httpx2.AsyncClient(timeout=10) as http:
        info = await http.get(endpoints.tokeninfo, headers={"Authorization": f"Bearer {token}"})
    return str(info.json().get("scope", "")).split() if info.status_code == 200 else None


async def _status(url: str, token: str) -> int:
    """The HTTP status the guard gives an MCP initialize with this token (401: refused)."""
    hello = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "check", "version": "1"},
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    async with httpx2.AsyncClient(timeout=30) as http:
        return (await http.post(url, json=hello, headers=headers)).status_code


def _serve(app: Any) -> tuple[str, uvicorn.Server]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the guard didn't start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/mcp", server


async def check(
    token: str,
    upstream_name: str,
    upstream: Upstream,
    *,
    domain: str | None = None,
    endpoints: GoogleEndpoints = GOOGLE,
) -> bool:
    """Runs the checks against a guard in --dev mode; True when the guard behaved correctly."""
    scopes = await _scopes(token, endpoints)
    if scopes is None:
        _out("Google doesn't accept this token (Playground tokens last an hour); copy a fresh one.")
        return False
    _out(f"Google says the token holds: {' '.join(sorted(scopes))}")
    allowed = {*upstream.google_scopes, EMAIL_SCOPE, "email", "openid"}
    needed = {*upstream.google_scopes, EMAIL_SCOPE}
    extra, missing = set(scopes) - allowed, needed - set(scopes)
    should_refuse = bool(extra or missing)
    if extra:
        _out(f"More than read-only access ({', '.join(sorted(extra))}): the guard must refuse it.")
    if missing:
        _out(f"Missing {', '.join(sorted(missing))}: the guard must refuse it.")

    env = {"MCP_GUARD_UPSTREAM": upstream_name}
    if domain:
        env["MCP_GUARD_ALLOWED_DOMAIN"] = domain
    settings = Settings.from_env(env, dev=True)
    url, server = _serve(build_app(settings, upstream, dev=True, endpoints=endpoints))
    _out(f"Guard ({upstream_name}, {upstream.kind}) running in --dev at {url}\n")
    results: list[tuple[bool, str]] = []
    try:
        status = await _status(url, token)
        if should_refuse:
            refused = status == 401
            verdict = "refused the token (401), as it should" if refused else f"answered {status}"
            results.append((refused, f"the guard {verdict}"))
        elif status != 200:
            results.append((False, f"the guard refused a read-only token ({status})"))
        else:
            transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
            async with Client(transport) as client:
                results.extend(await _read_only_checks(client, upstream))
    except Exception as exc:
        message = str(exc) or repr(exc)
        if domain and domain in message:
            message = f"the domain check refused this account: {message}"
        results.append((False, message))
    finally:
        server.should_exit = True

    _out()
    for ok, line in results:
        _out(f"{'PASS' if ok else 'FAIL'}  {line}")
    return all(ok for ok, _ in results)


async def _read_only_checks(client: Client[Any], upstream: Upstream) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    tools = {t.name for t in await client.list_tools()}
    results.append((tools == set(upstream.allow), f"tools offered: {', '.join(sorted(tools))}"))

    labels = await client.call_tool("list_labels", {})
    results.append((not labels.is_error, "list_labels works"))
    if upstream.kind == "gmail_api":
        names = [lb["name"] for lb in json.loads(labels.content[0].text)["labels"]]  # type: ignore[union-attr]
        _out(f"{len(names)} labels, e.g. {', '.join(names[:5])}")

    found = await client.call_tool("search_threads", {"query": "newer_than:30d", "max_results": 3})
    results.append((not found.is_error, "search_threads works"))
    if upstream.kind == "gmail_api":
        threads = json.loads(found.content[0].text)["threads"]  # type: ignore[union-attr]
        _out("Most recent threads (subjects only):")
        for thread in threads:
            _out(f"  - {thread['subject'] or '(no subject)'}")
    else:
        _out(f"search_threads answered {len(found.content[0].text)} characters")  # type: ignore[union-attr]

    for tool in WRITE_TOOLS:
        try:
            await client.call_tool(tool, {})
            results.append((False, f"{tool} was let through"))
        except Exception as exc:
            results.append(("Unknown tool" in str(exc), f"{tool} refused: {exc}"))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--upstream", default="gmail", help="entry in guard.yaml: gmail (default) or gmail-mcp"
    )
    parser.add_argument("--domain", help="your Workspace domain, to test the domain check too")
    args = parser.parse_args()
    token = os.environ.get("GOOGLE_ACCESS_TOKEN") or getpass.getpass(
        "OAuth Playground access token: "
    )
    upstream = GuardConfig.load(ROOT / "guard.yaml").upstream(args.upstream)
    configure_logging()  # the guard's audit lines and refusals show up as the checks run
    for noisy in ("httpx2", "mcp", "fastmcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ok = asyncio.run(check(token.strip(), args.upstream, upstream, domain=args.domain))
    _out("\nAll checks passed." if ok else "\nSome checks failed.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
