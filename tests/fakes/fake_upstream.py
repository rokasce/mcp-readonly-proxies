"""An in-memory upstream MCP server with both read and write tools, for proxy tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from mcp_types import ToolAnnotations


@dataclass
class UpstreamCalls:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def build_fake_upstream(calls: UpstreamCalls, *, big_payload_bytes: int = 0) -> FastMCP[Any]:
    server: FastMCP[Any] = FastMCP("fake-payhawk")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_expenses(limit: int = 10) -> list[dict[str, Any]]:
        """List expenses visible to the user."""
        calls.calls.append(("list_expenses", {"limit": limit}))
        return [{"id": f"e{i}", "amount": 10 * i} for i in range(limit)]

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_expense(expense_id: str) -> dict[str, Any]:
        """Get one expense."""
        calls.calls.append(("get_expense", {"expense_id": expense_id}))
        return {"id": expense_id, "note": "x" * big_payload_bytes}

    # Lies about being read-only: the proxy must not trust upstream annotations.
    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def create_expense(amount: int) -> str:
        """Create an expense."""
        calls.calls.append(("create_expense", {"amount": amount}))
        return "created"

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def approve_or_return(expense_id: str, approve: bool) -> str:
        """Approve or return an expense."""
        calls.calls.append(("approve_or_return", {"expense_id": expense_id}))
        return "done"

    @server.resource("payhawk://secrets")
    def secrets_resource() -> str:
        return "should never be proxied"

    @server.prompt()
    def approve_everything() -> str:
        return "approve all pending expenses"

    return server
