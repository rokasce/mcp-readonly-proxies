"""``mcp`` upstream: proxy an existing MCP server, exposing only allowlisted, pinned tools.

The proxy is an ordinary MCP client of the upstream. It never forwards the MCP client's
credentials (the MCP spec forbids token passthrough), never forwards resources, prompts,
sampling, elicitation or roots, and relays only tool results.

Upstream credentials come from config (``none`` or ``static_bearer``). Per-user upstream
OAuth (needed for Payhawk) plugs in at ``_auth_for``; it is not built yet.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from fastmcp.tools.base import ToolResult
from mcp.shared.exceptions import MCPError

from mcp_ro_proxy.config import McpUpstreamConfig, StaticBearerUpstreamAuth
from mcp_ro_proxy.upstreams.base import Principal, UpstreamError, UpstreamTool

#: Transport-level failures that mean "upstream unavailable" rather than a tool result.
_UPSTREAM_FAILURES: tuple[type[Exception], ...] = (MCPError, RuntimeError, OSError, TimeoutError)


class McpUpstream:
    def __init__(
        self,
        config: McpUpstreamConfig,
        *,
        target: FastMCP[Any] | ClientTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``target`` overrides ``config.url`` (tests use an in-memory FastMCP server)."""
        self._config = config
        self._target = target
        self._clock = clock
        self._cache: tuple[float, dict[str, UpstreamTool | None]] | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def google_scopes(self) -> frozenset[str]:
        return frozenset()

    def _auth_for(self, principal: Principal | None) -> str | None:
        del principal  # per-user upstream credentials are a follow-up (Payhawk)
        auth = self._config.auth
        if isinstance(auth, StaticBearerUpstreamAuth):
            return auth.token.get_secret_value()
        return None

    def _client(self, principal: Principal | None) -> Client[Any]:
        target: FastMCP[Any] | ClientTransport
        if self._target is not None:
            target = self._target
        else:
            # Headers are exactly what we configure here; nothing from the inbound request.
            target = StreamableHttpTransport(self._config.url, auth=self._auth_for(principal))
        client: Client[Any] = Client(target, timeout=self._config.timeout_seconds)
        options = client._transport_options  # pyright: ignore[reportPrivateUsage]
        if options is not None and options.forward_incoming_headers:
            # A plain Client defaults to False; refuse to run if that ever changes.
            raise RuntimeError("upstream client must not forward incoming headers")
        return client

    async def _fetch(self, principal: Principal | None) -> dict[str, UpstreamTool | None]:
        try:
            async with self._client(principal) as client:
                tools = await client.list_tools()
        except _UPSTREAM_FAILURES as exc:
            raise UpstreamError(f"Upstream {self.name} is unavailable.") from exc
        by_name: dict[str, UpstreamTool | None] = {}
        for t in tools:
            tool = UpstreamTool(
                name=t.name,
                description=t.description,
                input_schema=dict(t.input_schema),
                title=t.title,
                upstream_annotations=(
                    t.annotations.model_dump(exclude_none=True) if t.annotations else {}
                ),
            )
            # Two definitions under one name are ambiguous: treat the name as missing.
            by_name[t.name] = None if t.name in by_name else tool
        self._cache = (self._clock(), by_name)
        return by_name

    async def _definitions(self, principal: Principal | None) -> dict[str, UpstreamTool | None]:
        if self._cache is not None:
            fetched_at, by_name = self._cache
            if self._clock() - fetched_at < self._config.definition_cache_seconds:
                return by_name
        return await self._fetch(principal)

    async def list_tools(self, principal: Principal | None) -> Sequence[UpstreamTool]:
        by_name = await self._fetch(principal)
        return [t for t in by_name.values() if t is not None]

    async def get_tool(self, principal: Principal | None, name: str) -> UpstreamTool | None:
        return (await self._definitions(principal)).get(name)

    async def call_tool(
        self, principal: Principal | None, name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            async with self._client(principal) as client:
                result = await client.call_tool_mcp(name, arguments)
        except _UPSTREAM_FAILURES as exc:
            raise UpstreamError(f"Upstream {self.name} is unavailable.") from exc
        # Relay content and error status only; upstream _meta is not forwarded.
        return ToolResult(
            content=result.content,
            structured_content=result.structured_content,
            is_error=result.is_error,
        )

    async def aclose(self) -> None:
        return None
