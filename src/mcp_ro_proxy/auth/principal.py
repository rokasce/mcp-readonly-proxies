"""Map whatever authenticated the MCP request to the ``Principal`` the policy core uses.

Every client-auth mode (personal keys today, an OAuth authorization server later) puts the
same claims on FastMCP's ``AccessToken``: ``sub``, ``email``, ``client`` and optionally
``credential_id``.
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

from mcp_ro_proxy.upstreams.base import Principal


def principal_from_access_token(token: AccessToken | None) -> Principal | None:
    if token is None:
        return None
    claims = token.claims
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    credential = claims.get("credential_id")
    return Principal(
        sub=sub,
        email=str(claims.get("email", "")),
        client=str(claims.get("client", "unknown")),
        credential_id=str(credential) if credential is not None else None,
    )


def current_principal() -> Principal | None:
    """The authenticated end user of the MCP request being handled, if any."""
    return principal_from_access_token(get_access_token())
