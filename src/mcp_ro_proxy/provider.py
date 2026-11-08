"""FastMCP provider that exposes an upstream's tools through the policy engine.

Listing returns only tools that are allowlisted and whose current definition matches the
pin, advertised with the annotations from config. Each exposed tool forwards calls to the
upstream; the call-time checks live in ``PolicyMiddleware`` so they run independently of
how the tool was found.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from pydantic import PrivateAttr

from mcp_ro_proxy.audit import AuditLogger
from mcp_ro_proxy.auth.principal import current_principal
from mcp_ro_proxy.config import ToolRule
from mcp_ro_proxy.policy import PolicyEngine, Reason, annotations_for
from mcp_ro_proxy.upstreams.base import Upstream, UpstreamError, UpstreamTool

logger = logging.getLogger(__name__)


class PolicyTool(Tool):
    """An allowlisted, pinned upstream tool as exposed to MCP clients."""

    _upstream: Upstream = PrivateAttr()

    @classmethod
    def build(cls, upstream: Upstream, tool: UpstreamTool, rule: ToolRule) -> PolicyTool:
        exposed = cls(
            name=tool.name,
            title=rule.title or tool.title,
            description=tool.description,
            parameters=tool.input_schema,
            annotations=annotations_for(rule),
        )
        exposed._upstream = upstream
        return exposed

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            return await self._upstream.call_tool(current_principal(), self.name, arguments)
        except UpstreamError as exc:
            # UpstreamError messages are written for the client; FastMCP masks other errors.
            raise ToolError(str(exc)) from exc


class PolicyProvider(Provider):
    def __init__(self, upstream: Upstream, engine: PolicyEngine, audit: AuditLogger) -> None:
        super().__init__()
        self._upstream = upstream
        self._engine = engine
        self._audit = audit

    async def _list_tools(self) -> Sequence[Tool]:
        principal = current_principal()
        exposed, dropped = self._engine.partition(await self._upstream.list_tools(principal))
        for name, decision in dropped:
            if decision.reason is Reason.NOT_ALLOWLISTED:
                logger.debug("hiding tool %s: not allowlisted", name)
                continue
            # Pin drift or a vanished allowlisted tool needs a human: alert on these.
            self._audit.emit(
                "tool_hidden",
                principal=principal,
                severity="WARNING",
                tool=name,
                reason=decision.reason.value,
                pinned_sha256=decision.rule.sha256 if decision.rule else None,
                actual_sha256=decision.actual_sha256,
            )
        return [PolicyTool.build(self._upstream, tool, rule) for tool, rule in exposed]

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        # Proxied tools never run as background tasks; skip the startup upstream listing.
        return []

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        del version
        decision_tool = await self._upstream.get_tool(current_principal(), name)
        decision = self._engine.evaluate(name, decision_tool)
        if not decision.allowed or decision_tool is None or decision.rule is None:
            return None
        return PolicyTool.build(self._upstream, decision_tool, decision.rule)
