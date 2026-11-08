"""Structured JSON audit log, one line per decision.

Lines go to the ``mcp_ro_proxy.audit`` logger as JSON with a Cloud Logging compatible
``severity`` field. They never contain message bodies, tool arguments, tool results or
credentials: arguments are represented only by a sha256 of their canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

from mcp_ro_proxy.policy import canonical_json
from mcp_ro_proxy.upstreams.base import Principal

AUDIT_LOGGER_NAME = "mcp_ro_proxy.audit"


def args_sha256(arguments: dict[str, Any] | None) -> str:
    return hashlib.sha256(canonical_json(arguments or {}).encode("utf-8")).hexdigest()


class AuditLogger:
    def __init__(
        self,
        upstream: str,
        *,
        log_user_email: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._upstream = upstream
        self._log_user_email = log_user_email
        self._logger = logger or logging.getLogger(AUDIT_LOGGER_NAME)

    def _user(self, principal: Principal | None) -> dict[str, Any] | None:
        if principal is None:
            return None
        user: dict[str, Any] = {"sub": principal.sub}
        if self._log_user_email:
            user["email"] = principal.email
        return user

    def emit(
        self,
        event: str,
        *,
        principal: Principal | None = None,
        severity: str = "INFO",
        **fields: Any,
    ) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "severity": severity,
            "event": event,
            "upstream": self._upstream,
        }
        user = self._user(principal)
        if user is not None:
            record["user"] = user
        if principal is not None:
            record["client"] = principal.client
            if principal.credential_id is not None:
                record["credential_id"] = principal.credential_id
        record.update({k: v for k, v in fields.items() if v is not None})
        level = logging.WARNING if severity in {"WARNING", "ERROR"} else logging.INFO
        self._logger.log(level, json.dumps(record, sort_keys=True, default=str))

    def tool_call(
        self,
        *,
        principal: Principal | None,
        tool: str,
        decision: str,
        reason: str,
        arguments: dict[str, Any] | None,
        started: float,
        upstream_status: str | None = None,
        result_bytes: int | None = None,
    ) -> None:
        self.emit(
            "tool_call",
            principal=principal,
            severity="INFO" if decision == "allow" else "WARNING",
            method="tools/call",
            tool=tool,
            decision=decision,
            reason=reason,
            args_sha256=args_sha256(arguments),
            upstream_status=upstream_status,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            result_bytes=result_bytes,
        )


class JsonLineFormatter(logging.Formatter):
    """Pass audit JSON through untouched; wrap other log records as JSON too."""

    def format(self, record: logging.LogRecord) -> str:
        if record.name == AUDIT_LOGGER_NAME:
            return record.getMessage()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
