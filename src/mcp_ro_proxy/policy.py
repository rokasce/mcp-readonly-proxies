"""Default-deny allowlist with pinned tool definitions.

A tool is exposed or callable only when it is allowlisted in config *and* the upstream's
current definition hashes to the pinned value. The hash covers the name, the description
and the input schema, so a read tool that quietly gains a new parameter or a changed
description stops being exposed until someone reviews it and updates the pin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mcp_types import ToolAnnotations

from mcp_ro_proxy.config import ToolRule
from mcp_ro_proxy.upstreams.base import UpstreamTool


class Reason(StrEnum):
    ALLOWED = "allowed"
    NOT_ALLOWLISTED = "not_allowlisted"
    PIN_MISMATCH = "pin_mismatch"
    UPSTREAM_MISSING = "upstream_missing"
    INVALID_ARGUMENTS = "invalid_arguments"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_METHOD = "unsupported_method"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Reason
    rule: ToolRule | None = None
    actual_sha256: str | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def definition_sha256(name: str, description: str | None, input_schema: dict[str, Any]) -> str:
    """Hash of the parts of a tool definition that decide what it can do."""
    payload = {"name": name, "description": description or "", "inputSchema": input_schema}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def tool_sha256(tool: UpstreamTool) -> str:
    return definition_sha256(tool.name, tool.description, tool.input_schema)


def annotations_for(rule: ToolRule) -> ToolAnnotations:
    """The annotations to advertise, taken from config and never from upstream."""
    a = rule.annotations
    return ToolAnnotations(
        title=rule.title,
        read_only_hint=a.readOnlyHint,
        destructive_hint=a.destructiveHint,
        idempotent_hint=a.idempotentHint,
        open_world_hint=a.openWorldHint,
    )


class PolicyEngine:
    def __init__(self, rules: Iterable[ToolRule]) -> None:
        self._rules = {rule.name: rule for rule in rules}

    @property
    def allowlisted(self) -> frozenset[str]:
        return frozenset(self._rules)

    def rule(self, name: str) -> ToolRule | None:
        return self._rules.get(name)

    def evaluate(self, name: str, current: UpstreamTool | None) -> Decision:
        """Decide whether ``name`` may be exposed or called given its current definition."""
        rule = self._rules.get(name)
        if rule is None:
            return Decision(False, Reason.NOT_ALLOWLISTED)
        if current is None or current.name != name:
            return Decision(False, Reason.UPSTREAM_MISSING, rule)
        actual = tool_sha256(current)
        if actual != rule.sha256:
            return Decision(False, Reason.PIN_MISMATCH, rule, actual)
        return Decision(True, Reason.ALLOWED, rule, actual)

    def partition(
        self, tools: Sequence[UpstreamTool]
    ) -> tuple[list[tuple[UpstreamTool, ToolRule]], list[tuple[str, Decision]]]:
        """Split an upstream listing into exposed tools and dropped (name, decision) pairs.

        Allowlisted tools that the upstream no longer reports are included in the dropped
        list as ``upstream_missing`` so operators notice them.
        """
        exposed: list[tuple[UpstreamTool, ToolRule]] = []
        dropped: list[tuple[str, Decision]] = []
        seen: set[str] = set()
        for tool in tools:
            if tool.name in seen:
                # Ambiguous upstream listing: never pick one of two definitions.
                dropped.append((tool.name, Decision(False, Reason.PIN_MISMATCH)))
                exposed = [(t, r) for t, r in exposed if t.name != tool.name]
                continue
            seen.add(tool.name)
            decision = self.evaluate(tool.name, tool)
            if decision.allowed and decision.rule is not None:
                exposed.append((tool, decision.rule))
            else:
                dropped.append((tool.name, decision))
        for name, rule in self._rules.items():
            if name not in seen:
                dropped.append((name, Decision(False, Reason.UPSTREAM_MISSING, rule)))
        return exposed, dropped
