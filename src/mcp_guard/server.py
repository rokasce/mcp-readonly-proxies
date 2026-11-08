"""MCP guard: shows and forwards only allowed, read-only tools of one upstream.

Agents connect to the guard instead of the upstream. Each person signs in with Google (the guard
is an OAuth proxy to Google; MCP clients that take an API key use a personal key instead), and
the guard calls the upstream with that person's own Google token, so each person reaches only
their own data. Two independent limits:
- the allowlist (guard.yaml): other tools are left out of tools/list and refused on tools/call;
- the Google scopes (read-only): a token holding anything more is refused, so Google refuses
  any change even if a tool were allowed by mistake.
The upstream is either Gmail's REST API, through the guard's own read-only tools (gmail_api), or
an MCP server such as Google's Gmail MCP (mcp).
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import NotFoundError, ToolError, to_mcp_error
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, RateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient
from fastmcp.server.transforms.visibility import Visibility
from fastmcp.tools import Tool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_guard.config import EMAIL_SCOPE, IDENTITY_SCOPES, LOOPBACK, GuardConfig, Settings, Upstream
from mcp_guard.gmail_api import add_gmail_tools
from mcp_guard.google import (
    GOOGLE,
    GoogleClient,
    GoogleEndpoints,
    GoogleSignIn,
    ScopeLimitedTokenVerifier,
    in_domain,
)
from mcp_guard.personal_keys import PersonalKeys, WithPersonalKeys
from mcp_guard.storage import build_storage

logger = logging.getLogger("mcp_guard")
audit_logger = logging.getLogger("mcp_guard.audit")

ClientFactory = Callable[[], Client[Any]]
# Longest a single upstream request may take; the transport's own default read timeout is 5 minutes.
UPSTREAM_TIMEOUT_S = 60


def _list_error(error: ToolError) -> Exception:
    """tools/list masks every error but an MCPError as "Internal server error"; this keeps the
    message, so the agent learns why there are no tools."""
    return to_mcp_error(error)


def _person() -> dict[str, Any]:
    """The signed-in person's Google claims (email, email_verified, ...); empty if not signed in."""
    token = get_access_token()
    return (token.claims or {}) if token else {}


class DomainCheck(Middleware):
    """Refuses anyone who isn't a verified Google Workspace account in `domain` (see
    google.in_domain), before any tool is listed or called."""

    def __init__(self, domain: str):
        self._domain = domain.lower()

    def _check(self) -> None:
        person = _person()
        if not in_domain(person, self._domain):
            email = person.get("email") or "-"
            logger.warning("refused person=%s: not a verified %s account", email, self._domain)
            raise ToolError(f"Only {self._domain} accounts can use this server.")

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        try:
            self._check()
        except ToolError as error:
            raise _list_error(error) from None
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        self._check()
        return await call_next(context)


class Audit(Middleware):
    """One JSON line per tool call, after it ran: who, which tool, whether it was allowed, how it
    ended and how long it took. Arguments and results aren't logged: for Gmail they're search
    queries, ids and mail."""

    def __init__(self, upstream_name: str):
        self._upstream_name = upstream_name

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        started = time.monotonic()
        outcome = "error"
        try:
            result = await call_next(context)
            outcome = "error" if getattr(result, "is_error", False) else "ok"
            return result
        except NotFoundError:
            outcome = "refused"  # not allowed, or its pinned definition changed
            raise
        except RateLimitError:
            outcome = "rate_limited"
            raise
        finally:
            person = _person()
            audit_logger.info(
                json.dumps(
                    {
                        "event": "tool_call",
                        "upstream": self._upstream_name,
                        "person": person.get("email") or "-",
                        "client": person.get("client") or ("oauth" if person else "-"),
                        "tool": context.message.name,
                        "decision": "deny" if outcome in ("refused", "rate_limited") else "allow",
                        "outcome": outcome,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                )
            )


def definition_sha256(tool: Tool) -> str:
    """Hash of what decides what a tool does: its name, description and input schema."""
    payload = {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.parameters,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Pins(Middleware):
    """Drops a pinned tool whose definition changed upstream, from tools/list and tools/call,
    until someone reviews the change and updates the pin. The log line has the new hash."""

    def __init__(self, upstream_name: str, pins: dict[str, str]):
        self._upstream_name = upstream_name
        self._pins = pins

    def _drifted(self, tool: Tool) -> bool:
        pin = self._pins.get(tool.name)
        if pin is None:
            return False
        actual = definition_sha256(tool)
        if actual != pin:
            logger.warning(
                "upstream=%s tool=%s dropped: its definition changed (pinned %s, now %s)",
                self._upstream_name,
                tool.name,
                pin,
                actual,
            )
        return actual != pin

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tools: Sequence[Tool] = await call_next(context)
        return [tool for tool in tools if not self._drifted(tool)]

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        name = context.message.name
        if name in self._pins:
            server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
            tool = await server.get_tool(name) if server else None
            if tool is not None and self._drifted(tool):
                raise NotFoundError(f"Unknown tool: {name!r}")
        return await call_next(context)


class UpstreamCheck(Middleware):
    """Turns "the upstream couldn't be reached" into an error the agent can read. Otherwise FastMCP
    answers tools/list with no tools and an allowed call with "Unknown tool", which reads as if
    the server had nothing to offer rather than being temporarily unavailable."""

    def __init__(self, upstream_name: str, allow: set[str]):
        self._upstream_name = upstream_name
        self._allow = allow

    def _unavailable(self, what: str) -> ToolError:
        logger.warning("upstream=%s unavailable: %s", self._upstream_name, what)
        return ToolError(
            f"Couldn't reach {self._upstream_name}, or it no longer offers {what}. Try again later."
        )

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tools = await call_next(context)
        if not tools:
            raise _list_error(self._unavailable("any of the allowed tools"))
        return tools

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tool = context.message.name
        try:
            return await call_next(context)
        except NotFoundError:
            if tool in self._allow:
                raise self._unavailable(tool) from None
            raise


def user_token_client(upstream: Upstream) -> ClientFactory:
    """A new upstream client per request, calling as the signed-in person with their Google token.
    (Not one shared client: each request carries a different person's token.)"""

    def factory() -> Client[Any]:
        token = get_access_token()
        if token is None:
            raise ToolError("Not signed in.")
        transport = StreamableHttpTransport(
            str(upstream.url), headers={"Authorization": f"Bearer {token.token}"}
        )
        # ProxyClient forwards the agent's own request headers by default; an X-API-Key personal
        # key or anything else the agent sent must never reach the upstream.
        return ProxyClient(transport, timeout=UPSTREAM_TIMEOUT_S, forward_incoming_headers=False)

    return factory


def google_auth(
    settings: Settings, upstream: Upstream, *, endpoints: GoogleEndpoints = GOOGLE
) -> tuple[AuthProvider, list[Route]]:
    """The sign-in for a deployment: OAuth for Claude, plus personal keys if they're turned on.
    Returns the auth provider and the personal-key page's routes."""
    # Settings.from_env requires all four outside --dev.
    assert settings.base_url  # noqa: S101
    assert settings.google_client_id  # noqa: S101
    assert settings.google_client_secret  # noqa: S101
    assert settings.allowed_domain  # noqa: S101
    storage = build_storage(
        settings.storage_url,
        settings.storage_encryption_keys,
        settings.jwt_signing_key or settings.google_client_secret,
    )
    verifier = ScopeLimitedTokenVerifier(
        upstream.google_scopes,
        required=IDENTITY_SCOPES,
        client_id=settings.google_client_id,
        revoke_refused=True,
        endpoints=endpoints,
    )
    base_url = str(settings.base_url).rstrip("/")
    sign_in = GoogleSignIn(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=base_url,
        verifier=verifier,
        allowed_domain=settings.allowed_domain,
        client_redirect_uris=settings.client_redirect_uris,
        client_storage=storage,
        jwt_signing_key=settings.jwt_signing_key,
        endpoints=endpoints,
    )
    if not settings.personal_keys:
        return sign_in, []
    keys = PersonalKeys(
        store=storage,
        google=GoogleClient(settings.google_client_id, settings.google_client_secret, endpoints),
        verifier=verifier,
        base_url=base_url,
        upstream_scopes=upstream.google_scopes,
        allowed_domain=settings.allowed_domain,
        name=f"{settings.upstream_name}-guard",
    )
    return WithPersonalKeys(server=sign_in, verifiers=[keys]), keys.routes()


def dev_auth(upstream: Upstream, *, endpoints: GoogleEndpoints = GOOGLE) -> AuthProvider:
    """Local test mode: each request brings its own Google token (from any OAuth client, e.g.
    OAuth Playground), limited to read-only scopes. A refused token is the person's own, so it
    isn't revoked."""
    return ScopeLimitedTokenVerifier(
        upstream.google_scopes, required=[EMAIL_SCOPE], endpoints=endpoints
    )


def create_guard(
    upstream_name: str,
    upstream: Upstream,
    *,
    client_factory: ClientFactory | None = None,
    auth: AuthProvider | None = None,
    allowed_domain: str | None = None,
    routes: Sequence[Route] = (),
) -> FastMCP[Any]:
    allow = set(upstream.allow)
    common: dict[str, Any] = {
        "name": f"{upstream_name}-guard",
        "instructions": f"{upstream_name}, read-only, limited to: {', '.join(sorted(allow))}.",
        "auth": auth,
        # The domain check runs first, so refused people never show up as calls.
        "middleware": [
            *([DomainCheck(allowed_domain)] if allowed_domain else []),
            Audit(upstream_name),
            RateLimitingMiddleware(
                max_requests_per_second=upstream.rate_limit.per_second,
                burst_capacity=upstream.rate_limit.burst,
                get_client_id=lambda _: _person().get("email") or "-",
            ),
            *([Pins(upstream_name, upstream.pins)] if upstream.pins else []),
            UpstreamCheck(upstream_name, allow),
            ResponseLimitingMiddleware(max_size=upstream.max_response_bytes),
        ],
        "mask_error_details": True,
    }
    guard: FastMCP[Any]
    if upstream.kind == "mcp":
        guard = FastMCPProxy(client_factory=client_factory or user_token_client(upstream), **common)
    else:
        guard = FastMCP(**common)
        add_gmail_tools(guard, str(upstream.url))
    # Deny everything, then allow the listed tools: others are hidden from tools/list and refused
    # on tools/call.
    guard.add_transform(Visibility(False, match_all=True))
    guard.add_transform(Visibility(True, names=allow, components={"tool"}))

    @guard.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # Liveness only: needs no sign-in and doesn't call the upstream.
        return JSONResponse({"status": "ok", "upstream": upstream_name})

    for route in routes:
        guard.custom_route(route.path, methods=sorted(route.methods or []))(route.endpoint)
    return guard


def build_app(
    settings: Settings,
    upstream: Upstream,
    *,
    dev: bool = False,
    endpoints: GoogleEndpoints = GOOGLE,
) -> StarletteWithLifespan:
    """The guard as an ASGI app: MCP on /mcp, the sign-in routes, /health and, with personal keys
    on, the /keys page."""
    routes: list[Route] = []
    if dev:
        auth = dev_auth(upstream, endpoints=endpoints)
    else:
        auth, routes = google_auth(settings, upstream, endpoints=endpoints)
    guard = create_guard(
        settings.upstream_name,
        upstream,
        auth=auth,
        allowed_domain=settings.allowed_domain,
        routes=routes,
    )
    return guard.http_app(
        # No per-replica MCP session state, so any replica can answer any request.
        stateless_http=True,
        # Checks Host and Origin when the guard listens on localhost (--dev, local runs), so a
        # web page can't reach it through DNS rebinding.
        host_origin_protection="auto",
    )


class _AuditFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # The audit line is JSON already; it goes out as is, one object per line.
        return record.getMessage()


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_AuditFormatter())
    audit_logger.addHandler(handler)
    audit_logger.propagate = False


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-guard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=os.environ.get("MCP_GUARD_HOST"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_GUARD_PORT", "3000")))
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Local test mode: no sign-in; each request brings its own Google token "
        "(Authorization: Bearer), checked with Google and limited to the upstream's read-only "
        "scopes. Listens on 127.0.0.1 only.",
    )
    args = parser.parse_args()
    args.host = args.host or ("127.0.0.1" if args.dev else "0.0.0.0")  # noqa: S104 - a server
    if args.dev and args.host not in LOOPBACK:
        parser.error(
            f"--dev listens on this machine only (127.0.0.1, localhost or ::1), not {args.host}."
        )
    try:
        settings = Settings.from_env(dev=args.dev)
        upstream = GuardConfig.load(settings.config_path).upstream(settings.upstream_name)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.dev and settings.personal_keys:
        parser.error("Personal keys need the Google sign-in, which --dev doesn't run.")

    configure_logging()
    logger.info(
        "Guarding %s (%s at %s)%s; allowed: %s%s",
        settings.upstream_name,
        upstream.kind,
        upstream.url,
        " in local test mode (bring your own token)" if args.dev else "",
        ", ".join(upstream.allow),
        "; personal keys on" if settings.personal_keys else "",
    )
    app = build_app(settings, upstream, dev=args.dev)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, server_header=False)


if __name__ == "__main__":
    main()
