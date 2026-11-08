"""Personal API key authentication for MCP clients such as nexos.ai custom MCP.

A user signs in on the proxy's web page with Google and creates a revocable personal key.
The MCP client sends it as ``Authorization: Bearer <key>`` or in one of the configured
API-key headers (``X-API-Key`` by default). Keys are looked up by sha256 hash; the proxy
never stores them in clear text.

This is one implementation of FastMCP's ``AuthProvider``. An OAuth authorization-server
mode for Claude custom connectors can be added as another provider without touching the
policy core, which only sees the resulting ``Principal``.
"""

from __future__ import annotations

from typing import Any, cast

from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_ro_proxy.store import Store

CLIENT_PERSONAL_KEY = "personal_key"


class ApiKeyHeaderMiddleware:
    """Present an API-key header as a bearer credential, so one verifier handles both.

    Only applies when the request has no Authorization header, so an explicit bearer
    credential always wins and is never combined with another header.
    """

    def __init__(self, app: ASGIApp, header_names: tuple[str, ...]) -> None:
        self.app = app
        self._names = tuple(h.lower().encode("latin-1") for h in header_names)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._names:
            headers: list[tuple[bytes, bytes]] = list(scope.get("headers", []))
            if not any(name.lower() == b"authorization" for name, _ in headers):
                for name, value in headers:
                    if name.lower() in self._names and value.strip():
                        headers.append((b"authorization", b"Bearer " + value.strip()))
                        scope = {**scope, "headers": headers}
                        break
        await self.app(scope, receive, send)


class PersonalKeyAuth(TokenVerifier):
    def __init__(self, store: Store, *, api_key_headers: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._store = store
        self._api_key_headers = api_key_headers

    async def verify_token(self, token: str) -> AccessToken | None:
        owner = await self._store.authenticate_key(token.strip())
        if owner is None:
            return None
        return AccessToken(
            # The raw key is deliberately not kept on the request; the key id identifies it.
            token=f"personal-key:{owner.key.id}",
            client_id=f"{CLIENT_PERSONAL_KEY}:{owner.key.id}",
            scopes=[],
            expires_at=owner.key.expires_at,
            claims={
                "sub": owner.sub,
                "email": owner.email,
                "client": CLIENT_PERSONAL_KEY,
                "credential_id": owner.key.id,
            },
        )

    def get_middleware(self) -> list[Any]:
        return [
            Middleware(ApiKeyHeaderMiddleware, header_names=self._api_key_headers),
            *cast(list[Any], super().get_middleware()),  # pyright: ignore[reportUnknownMemberType]
        ]
