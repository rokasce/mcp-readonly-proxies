"""The interface every upstream kind implements, and the caller identity passed to it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastmcp.tools.base import ToolResult


@dataclass(frozen=True)
class Principal:
    """The authenticated end user a request is made for.

    ``sub`` is the stable Google account id, ``client`` says how the caller
    authenticated (for example ``personal_key``) and ``credential_id`` names the
    specific credential (a personal key id) without revealing it.
    """

    sub: str
    email: str
    client: str
    credential_id: str | None = None


@dataclass(frozen=True)
class UpstreamTool:
    """A tool definition as the upstream reports it, before any policy is applied."""

    name: str
    description: str | None
    input_schema: dict[str, Any]
    title: str | None = None
    upstream_annotations: dict[str, Any] = field(default_factory=dict[str, Any])


class UpstreamError(Exception):
    """An upstream failed in a way the caller should see as a tool error.

    The message is shown to the MCP client, so it must not contain secrets.
    """


class Upstream(Protocol):
    """Something the policy core can list tools from and forward calls to."""

    @property
    def name(self) -> str: ...

    @property
    def google_scopes(self) -> frozenset[str]:
        """Google OAuth scopes this upstream needs on top of ``openid email``."""
        ...

    async def list_tools(self, principal: Principal | None) -> Sequence[UpstreamTool]: ...

    async def get_tool(self, principal: Principal | None, name: str) -> UpstreamTool | None:
        """Return the current definition of one tool, for call-time pin checks."""
        ...

    async def call_tool(
        self, principal: Principal | None, name: str, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def aclose(self) -> None: ...
