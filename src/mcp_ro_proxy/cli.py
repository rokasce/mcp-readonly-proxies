"""Command line: ``serve``, ``pins``, ``check-config`` and ``generate-key``."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from pydantic import TypeAdapter

from mcp_ro_proxy.app import build_proxy
from mcp_ro_proxy.audit import configure_logging
from mcp_ro_proxy.config import (
    ConfigError,
    McpUpstreamConfig,
    NativeUpstreamConfig,
    ToolRule,
    UpstreamConfig,
    expand_env,
    load_config,
)
from mcp_ro_proxy.crypto import generate_key
from mcp_ro_proxy.policy import PolicyEngine, Reason, tool_sha256
from mcp_ro_proxy.upstreams.base import UpstreamTool
from mcp_ro_proxy.upstreams.gmail_native import NATIVE_TOOLS
from mcp_ro_proxy.upstreams.mcp_upstream import McpUpstream


class _LenientEnv(Mapping[str, str]):
    """Environment view that expands unset variables to a placeholder (for ``pins``)."""

    def __getitem__(self, key: str) -> str:
        return os.environ.get(key) or f"unset-{key}"

    def __iter__(self) -> Any:
        return iter(os.environ)

    def __len__(self) -> int:
        return len(os.environ)

    def __contains__(self, key: object) -> bool:
        return True


async def _upstream_tools(upstream: NativeUpstreamConfig | McpUpstreamConfig) -> list[UpstreamTool]:
    if isinstance(upstream, NativeUpstreamConfig):
        return [t.definition() for t in NATIVE_TOOLS]
    return list(await McpUpstream(upstream).list_tools(None))


def cmd_pins(path: str) -> int:
    """Print the current definition hash of every upstream tool, marking policy status."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    expanded = expand_env(raw, _LenientEnv())
    upstream = TypeAdapter(UpstreamConfig).validate_python(expanded["upstream"])
    rules = TypeAdapter(tuple[ToolRule, ...]).validate_python(expanded.get("allow") or ())
    engine = PolicyEngine(rules)
    tools = asyncio.run(_upstream_tools(upstream))
    print(f"# {len(tools)} tools reported by upstream {upstream.name!r}", file=sys.stderr)
    for tool in sorted(tools, key=lambda t: t.name):
        decision = engine.evaluate(tool.name, tool)
        status = {
            Reason.ALLOWED: "allowlisted, pin ok",
            Reason.PIN_MISMATCH: "allowlisted, PIN MISMATCH (review the change)",
            Reason.NOT_ALLOWLISTED: "not allowlisted",
        }.get(decision.reason, decision.reason.value)
        hint = tool.upstream_annotations.get("read_only_hint")
        print(f"- name: {tool.name}")
        print(f"  sha256: {tool_sha256(tool)}")
        print(f"  # {status}; upstream readOnlyHint={hint}")
    return 0


def cmd_check(path: str) -> int:
    config = load_config(path)
    print(
        f"ok: upstream {config.upstream.name!r} ({config.upstream.kind}), "
        f"{len(config.allow)} allowlisted tools, client auth {config.client_auth.mode}"
    )
    return 0


def cmd_serve(path: str, host: str, port: int) -> int:
    configure_logging()
    proxy = build_proxy(load_config(path))
    uvicorn.run(proxy.app, host=host, port=port, log_config=None, server_header=False)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-ro-proxy", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the proxy over Streamable HTTP")
    serve.add_argument("--config", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))

    pins = sub.add_parser("pins", help="print current upstream tool hashes for the allowlist")
    pins.add_argument("--config", required=True)

    check = sub.add_parser("check-config", help="validate a config file and its environment")
    check.add_argument("--config", required=True)

    keygen = sub.add_parser("generate-key", help="print a new token-encryption key")
    keygen.add_argument("--kid", default=None, help="key id (default: random)")

    sub.add_parser("generate-session-secret", help="print a new session secret")

    args = parser.parse_args(argv)
    try:
        match args.command:
            case "serve":
                return cmd_serve(args.config, args.host, args.port)
            case "pins":
                return cmd_pins(args.config)
            case "check-config":
                return cmd_check(args.config)
            case "generate-key":
                print(generate_key(args.kid))
                return 0
            case "generate-session-secret":
                print(secrets.token_urlsafe(48))
                return 0
            case _:  # pragma: no cover - argparse enforces the choices
                parser.error(f"unknown command {args.command}")
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
