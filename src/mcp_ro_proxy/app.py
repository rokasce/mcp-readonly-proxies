"""Build one proxy deployment (one upstream per process) from its config."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

from mcp_ro_proxy.audit import AuditLogger
from mcp_ro_proxy.auth.google import GMAIL_READONLY, GoogleAccessTokens, GoogleOAuth
from mcp_ro_proxy.auth.personal_keys import PersonalKeyAuth
from mcp_ro_proxy.auth.web import WebUI
from mcp_ro_proxy.config import McpUpstreamConfig, NativeUpstreamConfig, ProxyConfig
from mcp_ro_proxy.crypto import TokenCipher
from mcp_ro_proxy.middleware import PolicyMiddleware
from mcp_ro_proxy.policy import PolicyEngine
from mcp_ro_proxy.provider import PolicyProvider
from mcp_ro_proxy.store import SqlStore, Store
from mcp_ro_proxy.upstreams.base import Upstream
from mcp_ro_proxy.upstreams.gmail_native import GmailNativeUpstream
from mcp_ro_proxy.upstreams.mcp_upstream import McpUpstream

SESSION_COOKIE = "mrp_session"
SESSION_MAX_AGE_SECONDS = 8 * 3600


def upstream_google_scopes(config: ProxyConfig) -> frozenset[str]:
    upstream = config.upstream
    if isinstance(upstream, NativeUpstreamConfig) and upstream.provider == "gmail":
        return frozenset({GMAIL_READONLY})
    return frozenset()


@dataclass
class Proxy:
    """Everything one deployment is made of; ``app`` is the ASGI application to serve."""

    config: ProxyConfig
    app: Starlette
    server: FastMCP[Any]
    store: Store
    upstream: Upstream
    oauth: GoogleOAuth
    tokens: GoogleAccessTokens | None


def build_proxy(
    config: ProxyConfig,
    *,
    store: Store | None = None,
    http: httpx2.AsyncClient | None = None,
    upstream_target: FastMCP[Any] | ClientTransport | None = None,
) -> Proxy:
    """Assemble the proxy. ``store``, ``http`` and ``upstream_target`` exist for tests."""
    http = http or httpx2.AsyncClient(timeout=30.0, follow_redirects=False)
    store = store or SqlStore(
        config.storage.database_url,
        TokenCipher(config.storage.encryption_keys.get_secret_value()),
    )
    base_url = config.server.public_base_url
    audit = AuditLogger(config.upstream.name, log_user_email=config.audit.log_user_email)

    scopes = upstream_google_scopes(config)
    oauth = GoogleOAuth(
        config.identity,
        upstream_scopes=scopes,
        redirect_uri=f"{base_url}/auth/callback",
        http=http,
    )
    tokens = GoogleAccessTokens(oauth, store, sign_in_url=base_url) if scopes else None

    upstream: Upstream
    match config.upstream:
        case NativeUpstreamConfig(provider="gmail") as native:
            if tokens is None:  # pragma: no cover - gmail always needs Google scopes
                raise RuntimeError("the Gmail upstream needs Google access tokens")
            upstream = GmailNativeUpstream(native.name, native.gmail, tokens=tokens, http=http)
        case McpUpstreamConfig() as remote:
            upstream = McpUpstream(remote, target=upstream_target)

    engine = PolicyEngine(config.allow)
    server: FastMCP[Any] = FastMCP(
        name=f"{config.upstream.name}-readonly",
        instructions=(
            f"Read-only access to {config.upstream.name}. Only reviewed read-only tools are "
            "available; anything that would send, create, change or delete is not offered."
        ),
        providers=[PolicyProvider(upstream, engine, audit)],
        middleware=[
            PolicyMiddleware(
                upstream, engine, audit, max_response_bytes=config.server.max_response_bytes
            )
        ],
        auth=PersonalKeyAuth(store, api_key_headers=config.client_auth.api_key_headers),
        mask_error_details=True,
    )

    web = WebUI(config, oauth=oauth, store=store, audit=audit, tokens=tokens)
    for route in web.routes():
        server.custom_route(route.path, methods=sorted(route.methods or []))(route.endpoint)

    app = server.http_app(
        path=config.server.mcp_path,
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=[config.server.public_host, *config.server.allowed_hosts],
        # Explicit, because behind a TLS-terminating proxy (Cloud Run) the request scheme
        # is http and the same-origin fallback would reject the browser's https Origin.
        allowed_origins=[config.server.public_base_url],
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=config.storage.session_secret.get_secret_value(),
                session_cookie=SESSION_COOKIE,
                max_age=SESSION_MAX_AGE_SECONDS,
                same_site="lax",
                https_only=config.server.is_https,
            )
        ],
    )

    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope_app: Starlette) -> AsyncGenerator[None]:
        async with inner_lifespan(scope_app):
            try:
                yield
            finally:
                await upstream.aclose()
                await store.aclose()
                await http.aclose()

    app.router.lifespan_context = lifespan
    return Proxy(
        config=config,
        app=app,
        server=server,
        store=store,
        upstream=upstream,
        oauth=oauth,
        tokens=tokens,
    )
