"""Call-time enforcement, response size cap and audit, as FastMCP middleware.

``tools/call`` is checked here independently of listing: the tool must be allowlisted,
the upstream's *current* definition must match the pin, and the arguments must validate
against that pinned schema. Otherwise the call fails as an unknown tool and nothing is
forwarded. ``tools/list`` is filtered again as a second layer, and resources, prompts and
resource templates are never exposed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

import mcp_types as mt
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.prompts.base import Prompt, PromptResult
from fastmcp.resources.base import Resource, ResourceResult
from fastmcp.resources.template import ResourceTemplate
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from jsonschema import SchemaError
from jsonschema.validators import validator_for
from mcp_types import TextContent

from mcp_ro_proxy.audit import AuditLogger
from mcp_ro_proxy.auth.principal import current_principal
from mcp_ro_proxy.policy import PolicyEngine, Reason
from mcp_ro_proxy.provider import PolicyTool
from mcp_ro_proxy.upstreams.base import Upstream, UpstreamError


def result_size(result: ToolResult) -> int:
    size = sum(len(block.model_dump_json(by_alias=True)) for block in result.content)
    if result.structured_content is not None:
        size += len(json.dumps(result.structured_content, ensure_ascii=False))
    return size


def validation_problem(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Return a short description of why ``arguments`` violate ``schema``, or None."""
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
    except SchemaError:
        return "the tool's input schema is invalid"
    error = next(iter(cls(schema).iter_errors(arguments)), None)
    if error is None:
        return None
    where = "/".join(str(p) for p in error.absolute_path) or "arguments"
    # Name the failing location and rule only; never echo argument values.
    return f"{where}: violates '{error.validator}'"


class PolicyMiddleware(Middleware):
    def __init__(
        self,
        upstream: Upstream,
        engine: PolicyEngine,
        audit: AuditLogger,
        *,
        max_response_bytes: int,
    ) -> None:
        self._upstream = upstream
        self._engine = engine
        self._audit = audit
        self._max_response_bytes = max_response_bytes

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        allowed = self._engine.allowlisted
        # Anything that did not come through the policy provider is dropped as well.
        exposed = [t for t in tools if isinstance(t, PolicyTool) and t.name in allowed]
        self._audit.emit(
            "tools_list",
            principal=current_principal(),
            method="tools/list",
            decision="allow",
            exposed=sorted(t.name for t in exposed),
        )
        return exposed

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        started = time.monotonic()
        principal = current_principal()
        name = context.message.name
        arguments: dict[str, Any] = dict(context.message.arguments or {})

        def deny(reason: Reason) -> None:
            self._audit.tool_call(
                principal=principal,
                tool=name,
                decision="deny",
                reason=reason.value,
                arguments=arguments,
                started=started,
            )

        if self._engine.rule(name) is None:
            deny(Reason.NOT_ALLOWLISTED)
            raise NotFoundError(f"Unknown tool: {name!r}")

        try:
            current = await self._upstream.get_tool(principal, name)
        except UpstreamError as exc:
            deny(Reason.UPSTREAM_MISSING)
            raise ToolError(str(exc)) from exc
        decision = self._engine.evaluate(name, current)
        if not decision.allowed or current is None:
            deny(decision.reason)
            raise NotFoundError(f"Unknown tool: {name!r}")

        problem = validation_problem(current.input_schema, arguments)
        if problem is not None:
            deny(Reason.INVALID_ARGUMENTS)
            raise ToolError(f"Invalid arguments: {problem}")

        try:
            result = await call_next(context)
        except ToolError:
            self._audit.tool_call(
                principal=principal,
                tool=name,
                decision="allow",
                reason=Reason.ALLOWED.value,
                arguments=arguments,
                started=started,
                upstream_status="error",
            )
            raise
        except Exception:
            self._audit.tool_call(
                principal=principal,
                tool=name,
                decision="allow",
                reason=Reason.ALLOWED.value,
                arguments=arguments,
                started=started,
                upstream_status="exception",
            )
            raise

        size = result_size(result)
        limit = (decision.rule.max_response_bytes if decision.rule else None) or (
            self._max_response_bytes
        )
        if size > limit:
            self._audit.tool_call(
                principal=principal,
                tool=name,
                decision="deny",
                reason=Reason.RESPONSE_TOO_LARGE.value,
                arguments=arguments,
                started=started,
                upstream_status="ok",
                result_bytes=size,
            )
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"The response was {size} bytes, over this proxy's limit of "
                            f"{limit} bytes. Narrow the request (fewer results, a more "
                            "specific query or a smaller max_body_chars)."
                        ),
                    )
                ],
                is_error=True,
            )

        self._audit.tool_call(
            principal=principal,
            tool=name,
            decision="allow",
            reason=Reason.ALLOWED.value,
            arguments=arguments,
            started=started,
            upstream_status="error_result" if result.is_error else "ok",
            result_bytes=size,
        )
        return result

    # --- everything that is not a tool is denied ------------------------------------

    def _deny_method(self, method: str) -> None:
        self._audit.emit(
            "method_denied",
            principal=current_principal(),
            severity="WARNING",
            method=method,
            decision="deny",
            reason=Reason.UNSUPPORTED_METHOD.value,
        )

    async def on_list_resources(
        self,
        context: MiddlewareContext[mt.ListResourcesRequest],
        call_next: CallNext[mt.ListResourcesRequest, Sequence[Resource]],
    ) -> Sequence[Resource]:
        return []

    async def on_list_resource_templates(
        self,
        context: MiddlewareContext[mt.ListResourceTemplatesRequest],
        call_next: CallNext[mt.ListResourceTemplatesRequest, Sequence[ResourceTemplate]],
    ) -> Sequence[ResourceTemplate]:
        return []

    async def on_list_prompts(
        self,
        context: MiddlewareContext[mt.ListPromptsRequest],
        call_next: CallNext[mt.ListPromptsRequest, Sequence[Prompt]],
    ) -> Sequence[Prompt]:
        return []

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        self._deny_method("resources/read")
        raise NotFoundError("Unknown resource")

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mt.GetPromptRequestParams],
        call_next: CallNext[mt.GetPromptRequestParams, PromptResult],
    ) -> PromptResult:
        self._deny_method("prompts/get")
        raise NotFoundError("Unknown prompt")
