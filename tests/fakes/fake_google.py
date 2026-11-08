"""A fake of the Google endpoints the guard uses: OAuth 2.0, tokeninfo, userinfo, revocation, the
Gmail REST API and Google's Gmail MCP server.

It behaves like Google where it matters to the guard: the client must authenticate, PKCE is
checked, tokeninfo reports the granted scopes and audience, userinfo reports the Workspace
domain (`hd`), revoking any token drops its whole grant, and Gmail (API and MCP) needs an access
token holding `gmail.readonly`. Gmail requests other than GET are refused and recorded, so tests
can assert the guard never attempted a write.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_guard.google import GoogleEndpoints

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
# Gmail MCP's tools/list (names and annotations), taken from https://gmailmcp.googleapis.com/mcp/v1.
GMAIL_MCP_TOOLS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "gmail_tools.json").read_text()
)["tools"]
# Query parameters Gmail binds for drafts.list and drafts.get; any other one is a 400.
_DRAFTS_LIST_PARAMS = frozenset({"maxResults", "pageToken", "q", "includeSpamTrash"})
_DRAFTS_GET_PARAMS = frozenset({"format"})


def endpoints_for(base_url: str) -> GoogleEndpoints:
    return GoogleEndpoints(
        authorize=f"{base_url}/o/oauth2/v2/auth",
        token=f"{base_url}/token",
        revoke=f"{base_url}/revoke",
        tokeninfo=f"{base_url}/tokeninfo",
        userinfo=f"{base_url}/oauth2/v2/userinfo",
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(
    mid: str,
    tid: str,
    *,
    subject: str,
    sender: str,
    to: str,
    date: str,
    labels: list[str],
    plain: str | None = None,
    html: str | None = None,
    attachment: str | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"data": _b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"data": _b64(html)}})
    if attachment is not None:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": attachment,
                "body": {"attachmentId": "att1", "size": 12345},
            }
        )
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Date", "value": date},
    ]
    return {
        "id": mid,
        "threadId": tid,
        "labelIds": labels,
        "snippet": (plain or "Hello &amp; welcome")[:60],
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


def default_mailbox() -> dict[str, list[dict[str, Any]]]:
    return {
        "t1": [
            _message(
                "m1",
                "t1",
                subject="Q3 invoice",
                sender="Ana <ana@vendor.test>",
                to="pat@example.com",
                date="Mon, 2 Nov 2026 09:00:00 +0000",
                labels=["INBOX", "UNREAD"],
                plain="Please find the Q3 invoice attached.",
                attachment="invoice-q3.pdf",
            ),
            _message(
                "m2",
                "t1",
                subject="Re: Q3 invoice",
                sender="Pat <pat@example.com>",
                to="ana@vendor.test",
                date="Mon, 2 Nov 2026 10:30:00 +0000",
                labels=["SENT"],
                html="<p>Thanks, <b>received</b>.</p><style>x{}</style>",
            ),
        ],
        "t2": [
            _message(
                "m3",
                "t2",
                subject="Team offsite",
                sender="Lee <lee@example.com>",
                to="team@example.com",
                date="Tue, 3 Nov 2026 08:15:00 +0000",
                labels=["INBOX"],
                plain="Offsite is on Friday.",
            ),
        ],
    }


DRAFT = _message(
    "m9",
    "t9",
    subject="Draft reply",
    sender="pat@example.com",
    to="ana@vendor.test",
    date="Wed, 4 Nov 2026 12:00:00 +0000",
    labels=["DRAFT"],
    plain="Draft body",
)


@dataclass
class FakeUser:
    sub: str = "1001"
    email: str = "pat@example.com"
    hd: str | None = "example.com"
    email_verified: bool = True


@dataclass
class Grant:
    scopes: list[str]
    user: FakeUser
    client_id: str
    revoked: bool = False


@dataclass
class FakeGoogle:
    """State of the fake. Tests tweak `user` and `grant_scopes` per scenario."""

    user: FakeUser = field(default_factory=FakeUser)
    # Scopes to grant instead of the requested ones (simulate a broad grant).
    grant_scopes: list[str] | None = None
    # How long access tokens last; Google's is an hour.
    access_lifetime_s: int = 3599
    # Status userinfo answers with instead of the profile (simulate an outage).
    userinfo_failure: int | None = None
    mailbox: dict[str, list[dict[str, Any]]] = field(default_factory=default_mailbox)
    authorize_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    gmail_requests: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    write_attempts: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    mcp_calls: list[str] = field(default_factory=list[str])
    mcp_headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    revocations: list[str] = field(default_factory=list[str])
    # grant_type of each token request.
    token_grants: list[str] = field(default_factory=list[str])
    _codes: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    _access: dict[str, tuple[Grant, float]] = field(default_factory=dict[str, tuple[Grant, float]])
    _refresh: dict[str, Grant] = field(default_factory=dict[str, Grant])

    # --- helpers ------------------------------------------------------------------------------

    def issue(
        self, scopes: list[str], *, client_id: str = CLIENT_ID, user: FakeUser | None = None
    ) -> str:
        """An access token as Google would issue it, without the browser sign-in."""
        return self._new_access(Grant(scopes, user or self.user, client_id))

    def _new_access(self, grant: Grant) -> str:
        token = "ya29.fake-" + secrets.token_urlsafe(16)
        self._access[token] = (grant, time.time() + self.access_lifetime_s)
        return token

    def grant_for(self, token: str) -> Grant | None:
        found = self._access.get(token)
        if found is None:
            grant = self._refresh.get(token)
        else:
            grant = found[0] if found[1] > time.time() else None
        return None if grant is None or grant.revoked else grant

    def is_revoked(self, token: str) -> bool:
        found = self._access.get(token)
        grant = found[0] if found else self._refresh.get(token)
        return grant is not None and grant.revoked

    def _bearer(self, request: Request) -> Grant | None:
        auth = request.headers.get("authorization", "")
        return self.grant_for(auth[7:]) if auth.startswith("Bearer ") else None

    # --- OAuth endpoints ----------------------------------------------------------------------

    async def authorize(self, request: Request) -> Response:
        params = dict(request.query_params)
        self.authorize_requests.append(params)
        code = "code-" + secrets.token_urlsafe(16)
        self._codes[code] = params
        q = urlencode({"code": code, "state": params.get("state", "")})
        return RedirectResponse(f"{params['redirect_uri']}?{q}", status_code=302)

    def _client(self, request: Request, form: dict[str, str]) -> str | None:
        """The authenticated client id: client_secret_post or client_secret_basic."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            client_id, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
        else:
            client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
        return client_id if (client_id, secret) == (CLIENT_ID, CLIENT_SECRET) else None

    async def token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        client_id = self._client(request, form)
        if client_id is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        self.token_grants.append(form.get("grant_type", ""))
        if form.get("grant_type") == "authorization_code":
            params = self._codes.pop(form.get("code", ""), None)
            if params is None or params["redirect_uri"] != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if base64.urlsafe_b64encode(digest).decode().rstrip("=") != params.get(
                "code_challenge"
            ):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "pkce"}, status_code=400
                )
            requested = [USERINFO_EMAIL if s == "email" else s for s in params["scope"].split()]
            grant = Grant(self.grant_scopes or requested, self.user, client_id)
            body: dict[str, Any] = {
                "access_token": self._new_access(grant),
                "expires_in": self.access_lifetime_s,
                "token_type": "Bearer",
                "scope": " ".join(grant.scopes),
            }
            if params.get("access_type") == "offline":
                refresh = "1//fake-" + secrets.token_urlsafe(16)
                self._refresh[refresh] = grant
                body["refresh_token"] = refresh
            return JSONResponse(body)
        if form.get("grant_type") == "refresh_token":
            grant = self._refresh.get(form.get("refresh_token", ""))
            if grant is None or grant.revoked:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(
                {
                    "access_token": self._new_access(grant),
                    "expires_in": self.access_lifetime_s,
                    "token_type": "Bearer",
                    "scope": " ".join(grant.scopes),
                }
            )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def tokeninfo(self, request: Request) -> Response:
        token = (
            request.query_params.get("access_token") or request.headers.get("authorization", "")[7:]
        )
        found = self._access.get(token)
        if found is None or found[0].revoked or found[1] < time.time():
            return JSONResponse({"error": "invalid_token"}, status_code=400)
        grant, expires = found
        return JSONResponse(
            {
                "aud": grant.client_id,
                "azp": grant.client_id,
                "sub": grant.user.sub,
                "scope": " ".join(grant.scopes),
                "expires_in": str(int(expires - time.time())),
                "email": grant.user.email,
                "email_verified": "true" if grant.user.email_verified else "false",
            }
        )

    async def userinfo(self, request: Request) -> Response:
        if self.userinfo_failure:
            return JSONResponse({"error": "backend_error"}, status_code=self.userinfo_failure)
        grant = self._bearer(request)
        if grant is None:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        user = grant.user
        data: dict[str, Any] = {
            "id": user.sub,
            "email": user.email,
            "verified_email": user.email_verified,
        }
        if user.hd:
            data["hd"] = user.hd
        return JSONResponse(data)

    async def revoke(self, request: Request) -> Response:
        token = str((await request.form()).get("token", ""))
        self.revocations.append(token)
        found = self._access.get(token)
        grant = found[0] if found else self._refresh.get(token)
        if grant is None:
            return JSONResponse({"error": "invalid_token"}, status_code=400)
        grant.revoked = True  # Google drops the whole grant: every access and refresh token
        return Response(status_code=200)

    # --- Gmail API ----------------------------------------------------------------------------

    async def gmail(self, request: Request) -> Response:
        path = request.path_params["path"]
        self.gmail_requests.append((request.method, path, str(request.query_params)))
        if request.method != "GET":
            self.write_attempts.append((request.method, path))
            return JSONResponse({"error": {"code": 405}}, status_code=405)
        grant = self._bearer(request)
        if grant is None:
            return JSONResponse({"error": {"code": 401}}, status_code=401)
        if GMAIL_READONLY not in grant.scopes:
            return JSONResponse({"error": {"code": 403}}, status_code=403)
        return self._gmail_get(path, request)

    def _gmail_get(self, path: str, request: Request) -> Response:
        parts = path.split("/")
        threads = self.mailbox
        if parts == ["threads"]:
            max_results = int(request.query_params.get("maxResults", "100"))
            ids = list(threads)
            q = request.query_params.get("q", "")
            if q:
                ids = [t for t in ids if any(q.lower() in str(m).lower() for m in threads[t])]
            listing = [{"id": t, "snippet": threads[t][-1]["snippet"]} for t in ids]
            return JSONResponse({"threads": listing[:max_results], "resultSizeEstimate": len(ids)})
        if len(parts) == 2 and parts[0] == "threads":
            if parts[1] not in threads:
                return JSONResponse({"error": {"code": 404}}, status_code=404)
            return JSONResponse({"id": parts[1], "messages": threads[parts[1]]})
        if len(parts) == 2 and parts[0] == "messages":
            for message in (m for msgs in threads.values() for m in msgs):
                if message["id"] == parts[1]:
                    return JSONResponse(message)
            return JSONResponse({"error": {"code": 404}}, status_code=404)
        if parts == ["labels"]:
            labels = [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "SENT", "name": "SENT", "type": "system"},
                {"id": "UNREAD", "name": "UNREAD", "type": "system"},
                {"id": "DRAFT", "name": "DRAFT", "type": "system"},
                {"id": "Label_1", "name": "Invoices", "type": "user"},
            ]
            return JSONResponse({"labels": labels})
        if parts and parts[0] == "drafts":
            allowed = _DRAFTS_LIST_PARAMS if len(parts) == 1 else _DRAFTS_GET_PARAMS
            unknown = sorted(set(request.query_params) - allowed)
            if unknown:
                message = f"Cannot bind query parameter {unknown[0]}"
                return JSONResponse({"error": {"code": 400, "message": message}}, status_code=400)
            if parts == ["drafts"]:
                return JSONResponse({"drafts": [{"id": "d1", "message": {"id": "m9"}}]})
            if parts == ["drafts", "d1"]:
                return JSONResponse({"id": "d1", "message": DRAFT})
        return JSONResponse({"error": {"code": 404}}, status_code=404)

    # --- Gmail MCP ----------------------------------------------------------------------------

    def gmail_mcp(self) -> FastMCP[Any]:
        """Google's Gmail MCP server: every real tool name, each needing a gmail.readonly token
        in Authorization (as the real one does). Records calls and the headers they came with."""
        server: FastMCP[Any] = FastMCP("fake-gmail-mcp")
        for spec in GMAIL_MCP_TOOLS:
            annotations = ToolAnnotations.model_validate(spec["annotations"])

            def tool(id: str = "", _name: str = spec["name"]) -> str:
                headers = get_http_headers(include_all=True)
                self.mcp_headers.append(headers)
                auth = headers.get("authorization", "")
                grant = self.grant_for(auth[7:]) if auth.startswith("Bearer ") else None
                if grant is None or GMAIL_READONLY not in grant.scopes:
                    raise ToolError("Request had invalid authentication credentials.")
                self.mcp_calls.append(_name)
                return f"{_name} ok"

            server.tool(tool, name=spec["name"], annotations=annotations)
        return server

    # --- app ----------------------------------------------------------------------------------

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/o/oauth2/v2/auth", self.authorize, methods=["GET"]),
                Route("/token", self.token, methods=["POST"]),
                Route("/tokeninfo", self.tokeninfo, methods=["GET", "POST"]),
                Route("/oauth2/v2/userinfo", self.userinfo, methods=["GET"]),
                Route("/revoke", self.revoke, methods=["POST"]),
                Route(
                    "/gmail/v1/users/me/{path:path}",
                    self.gmail,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                ),
            ]
        )
