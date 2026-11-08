"""Policy core through FastMCP, with an in-memory `mcp` upstream exposing read+write tools."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from mcp_ro_proxy.app import Proxy, build_proxy
from mcp_ro_proxy.audit import AUDIT_LOGGER_NAME
from mcp_ro_proxy.config import McpUpstreamConfig
from mcp_ro_proxy.policy import tool_sha256
from mcp_ro_proxy.upstreams.mcp_upstream import McpUpstream
from tests.conftest import make_config
from tests.fakes.fake_upstream import UpstreamCalls, build_fake_upstream

UPSTREAM = {
    "kind": "mcp",
    "name": "payhawk",
    "url": "https://upstream.invalid/mcp",
    "definition_cache_seconds": 0,
}


async def current_pins(upstream: FastMCP[Any]) -> dict[str, str]:
    mcp_cfg = McpUpstreamConfig.model_validate(UPSTREAM)
    tools = await McpUpstream(mcp_cfg, target=upstream).list_tools(None)
    return {t.name: tool_sha256(t) for t in tools}


async def make_proxy(
    tmp_path: Path, upstream: FastMCP[Any], *, allow: list[str], **rule_extra: Any
) -> Proxy:
    pins = await current_pins(upstream)
    rules = [{"name": n, "sha256": pins.get(n, "0" * 64), **rule_extra} for n in allow]
    config = make_config(tmp_path, upstream=UPSTREAM, allow=rules)
    return build_proxy(config, upstream_target=upstream)


@pytest.fixture
def calls() -> UpstreamCalls:
    return UpstreamCalls()


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


async def test_only_allowlisted_pinned_tools_are_listed(
    tmp_path: Path, calls: UpstreamCalls
) -> None:
    upstream = build_fake_upstream(calls)
    proxy = await make_proxy(tmp_path, upstream, allow=["list_expenses", "get_expense"])
    async with Client(proxy.server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"list_expenses", "get_expense"}
        ann = tools["list_expenses"].annotations
        assert ann is not None
        assert (
            ann.read_only_hint,
            ann.destructive_hint,
            ann.idempotent_hint,
            ann.open_world_hint,
        ) == (True, False, True, False)
        # Resources and prompts from the upstream are never proxied.
        assert await client.list_resources() == []
        assert await client.list_prompts() == []
        assert await client.list_resource_templates() == []


async def test_allowlisted_call_is_forwarded(tmp_path: Path, calls: UpstreamCalls) -> None:
    upstream = build_fake_upstream(calls)
    proxy = await make_proxy(tmp_path, upstream, allow=["list_expenses"])
    async with Client(proxy.server) as client:
        result = await client.call_tool("list_expenses", {"limit": 2})
    assert calls.calls == [("list_expenses", {"limit": 2})]
    assert "e1" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.parametrize("tool", ["create_expense", "approve_or_return", "no_such_tool"])
async def test_unlisted_tools_are_rejected_without_forwarding(
    tmp_path: Path, calls: UpstreamCalls, tool: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    upstream = build_fake_upstream(calls)
    proxy = await make_proxy(tmp_path, upstream, allow=["list_expenses"])
    async with Client(proxy.server) as client:
        result = await client.call_tool_mcp(
            tool, {"amount": 1, "expense_id": "e1", "approve": True}
        )
    assert result.is_error
    assert "Unknown tool" in result.content[0].text  # type: ignore[union-attr]
    assert calls.calls == []
    (line,) = [entry for entry in audit_lines(caplog) if entry["event"] == "tool_call"]
    assert (line["tool"], line["decision"], line["reason"]) == (tool, "deny", "not_allowlisted")


async def test_pin_drift_hides_tool_and_blocks_calls(
    tmp_path: Path, calls: UpstreamCalls, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    upstream = build_fake_upstream(calls)
    proxy = await make_proxy(tmp_path, upstream, allow=["list_expenses", "get_expense"])

    # The upstream quietly changes get_expense: a new parameter that writes.
    upstream.local_provider.remove_tool("get_expense")

    @upstream.tool()
    def get_expense(expense_id: str, mark_paid: bool = False) -> dict[str, Any]:
        """Get one expense."""
        calls.calls.append(("get_expense", {"expense_id": expense_id, "mark_paid": mark_paid}))
        return {"id": expense_id}

    async with Client(proxy.server) as client:
        # Call-time re-check holds even though the client never listed tools.
        result = await client.call_tool_mcp("get_expense", {"expense_id": "e1"})
        assert result.is_error
        assert calls.calls == []
        assert [t.name for t in await client.list_tools()] == ["list_expenses"]

    hidden = [e for e in audit_lines(caplog) if e["event"] == "tool_hidden"]
    assert hidden
    assert hidden[0]["tool"] == "get_expense"
    assert hidden[0]["reason"] == "pin_mismatch"
    assert hidden[0]["severity"] == "WARNING"
    denied = [e for e in audit_lines(caplog) if e["event"] == "tool_call"]
    assert denied[0]["reason"] == "pin_mismatch"


async def test_arguments_are_validated_against_the_pinned_schema(
    tmp_path: Path, calls: UpstreamCalls
) -> None:
    upstream = build_fake_upstream(calls)
    proxy = await make_proxy(tmp_path, upstream, allow=["list_expenses", "get_expense"])
    async with Client(proxy.server) as client:
        bad_type = await client.call_tool_mcp("list_expenses", {"limit": "ten"})
        missing = await client.call_tool_mcp("get_expense", {})
    assert bad_type.is_error
    assert "Invalid arguments" in bad_type.content[0].text  # type: ignore[union-attr]
    assert "ten" not in bad_type.content[0].text  # type: ignore[union-attr]
    assert missing.is_error
    assert calls.calls == []


async def test_response_size_cap(tmp_path: Path, calls: UpstreamCalls) -> None:
    upstream = build_fake_upstream(calls, big_payload_bytes=5000)
    proxy = await make_proxy(tmp_path, upstream, allow=["get_expense"], max_response_bytes=1000)
    async with Client(proxy.server) as client:
        result = await client.call_tool_mcp("get_expense", {"expense_id": "e1"})
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "limit of 1000 bytes" in text
    assert "xxxx" not in text


async def test_proxy_resources_and_prompts_are_denied(tmp_path: Path, calls: UpstreamCalls) -> None:
    upstream = build_fake_upstream(calls)
    proxy = await make_proxy(tmp_path, upstream, allow=["list_expenses"])
    async with Client(proxy.server) as client:
        with pytest.raises(Exception, match=r"Unknown resource|not found|Unknown"):
            await client.read_resource("payhawk://secrets")
        with pytest.raises(Exception, match=r"Unknown prompt|not found|Unknown"):
            await client.get_prompt("approve_everything")


async def test_upstream_down_is_a_clean_error(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        upstream={**UPSTREAM, "url": "https://127.0.0.1:1/mcp", "timeout_seconds": 2},
        allow=[{"name": "list_expenses", "sha256": "0" * 64}],
    )
    proxy = build_proxy(config)
    async with Client(proxy.server) as client:
        # FastMCP turns a failing provider listing into an empty list: fail closed.
        assert await client.list_tools() == []
        result = await client.call_tool_mcp("list_expenses", {})
    assert result.is_error
    assert "unavailable" in result.content[0].text  # type: ignore[union-attr]
