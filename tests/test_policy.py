from __future__ import annotations

from mcp_ro_proxy.config import ToolAnnotationsConfig, ToolRule
from mcp_ro_proxy.policy import (
    PolicyEngine,
    Reason,
    annotations_for,
    definition_sha256,
    tool_sha256,
)
from mcp_ro_proxy.upstreams.base import UpstreamTool

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def tool(name: str = "search", description: str = "Search", schema: dict | None = None):
    return UpstreamTool(name=name, description=description, input_schema=schema or SCHEMA)


def rule_for(t: UpstreamTool, **kwargs) -> ToolRule:
    return ToolRule(name=t.name, sha256=tool_sha256(t), **kwargs)


def test_hash_is_canonical_and_stable() -> None:
    a = definition_sha256("x", "d", {"b": 1, "a": [1, 2]})
    b = definition_sha256("x", "d", {"a": [1, 2], "b": 1})
    assert a == b
    # Known vector: protects pins in deployed configs from silent algorithm changes.
    # sha256 of '{"description":"","inputSchema":{},"name":"x"}'
    assert definition_sha256("x", None, {}) == (
        "c9e1174799ea1f7976d584a71522be868010192fb5c0970c8c94b5d667eeed68"
    )
    assert definition_sha256("x", None, {}) == definition_sha256("x", "", {})


def test_hash_covers_name_description_and_schema() -> None:
    base = tool_sha256(tool())
    assert tool_sha256(tool(name="search2")) != base
    assert tool_sha256(tool(description="Search and also send")) != base
    widened = {"type": "object", "properties": {"q": {"type": "string"}, "send": {}}}
    assert tool_sha256(tool(schema=widened)) != base


def test_evaluate_reasons() -> None:
    t = tool()
    engine = PolicyEngine([rule_for(t)])
    assert engine.evaluate("search", t).allowed
    assert engine.evaluate("send", tool(name="send")).reason is Reason.NOT_ALLOWLISTED
    assert engine.evaluate("search", None).reason is Reason.UPSTREAM_MISSING
    drifted = engine.evaluate("search", tool(description="changed"))
    assert drifted.reason is Reason.PIN_MISMATCH
    assert drifted.actual_sha256 == tool_sha256(tool(description="changed"))
    # A definition reported under another name never satisfies a rule.
    assert engine.evaluate("search", tool(name="other")).reason is Reason.UPSTREAM_MISSING


def test_partition_default_denies_and_reports_drift() -> None:
    read, write, drift = tool("read"), tool("write"), tool("drift")
    engine = PolicyEngine(
        [
            rule_for(read),
            ToolRule(name="drift", sha256="0" * 64),
            ToolRule(name="gone", sha256="1" * 64),
        ]
    )
    exposed, dropped = engine.partition([read, write, drift])
    assert [t.name for t, _ in exposed] == ["read"]
    reasons = {name: d.reason for name, d in dropped}
    assert reasons == {
        "write": Reason.NOT_ALLOWLISTED,
        "drift": Reason.PIN_MISMATCH,
        "gone": Reason.UPSTREAM_MISSING,
    }


def test_duplicate_upstream_names_are_never_exposed() -> None:
    a = tool("dup")
    b = tool("dup", description="evil twin")
    engine = PolicyEngine([rule_for(a)])
    exposed, dropped = engine.partition([a, b])
    assert exposed == []
    assert ("dup", Reason.PIN_MISMATCH) in [(n, d.reason) for n, d in dropped]


def test_annotations_come_from_config() -> None:
    rule = ToolRule(
        name="x",
        sha256="a" * 64,
        title="X tool",
        annotations=ToolAnnotationsConfig(openWorldHint=True, idempotentHint=False),
    )
    ann = annotations_for(rule)
    assert ann.read_only_hint is True
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False
    assert ann.open_world_hint is True
    assert ann.title == "X tool"
