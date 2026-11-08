"""A fake of the Google endpoints the proxy uses: OAuth 2.0 / OIDC and the Gmail REST API.

Used by the tests, and runnable for local end-to-end trials without a real Google OAuth
client::

    uv run python -m tests.fakes.fake_google --port 9000

It behaves like Google where it matters to the proxy: PKCE is checked, ID tokens are
RS256-signed and served through a JWKS endpoint, refresh and tokeninfo report the granted
scopes, revocation invalidates tokens, and Gmail endpoints require an access token that
holds ``gmail.readonly``. Gmail requests other than GET are refused and recorded, so tests
can assert the proxy never attempted a write.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"


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


@dataclass
class FakeUser:
    sub: str = "1001"
    email: str = "pat@example.com"
    hd: str | None = "example.com"
    email_verified: bool = True


@dataclass
class FakeGoogle:
    """State of the fake. Tests tweak ``user`` and ``grant_scopes`` per scenario."""

    user: FakeUser = field(default_factory=FakeUser)
    #: Scopes to grant instead of the requested ones (simulate a broad grant).
    grant_scopes: list[str] | None = None
    #: When set, the authorize endpoint returns this error instead of a code.
    consent_error: str | None = None
    mailbox: dict[str, list[dict[str, Any]]] = field(default_factory=default_mailbox)
    authorize_requests: list[dict[str, str]] = field(default_factory=list)
    gmail_requests: list[tuple[str, str, str]] = field(default_factory=list)
    gmail_authorizations: list[str] = field(default_factory=list)
    write_attempts: list[tuple[str, str]] = field(default_factory=list)
    revoked: set[str] = field(default_factory=set)
    _codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    _access: dict[str, dict[str, Any]] = field(default_factory=dict)
    _refresh: dict[str, dict[str, Any]] = field(default_factory=dict)
    issuer: str = "https://accounts.google.com"

    def __post_init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = secrets.token_hex(4)

    # --- helpers ---------------------------------------------------------------------

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        jwk.update({"kid": self._kid, "alg": "RS256", "use": "sig"})
        return {"keys": [jwk]}

    def _id_token(self, nonce: str, aud: str) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": aud,
            "azp": aud,
            "sub": self.user.sub,
            "email": self.user.email,
            "email_verified": self.user.email_verified,
            "iat": now,
            "exp": now + 3600,
            "nonce": nonce,
        }
        if self.user.hd is not None:
            claims["hd"] = self.user.hd
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self._kid})

    def _issue_access(self, scopes: list[str], client_id: str) -> str:
        token = "ya29.fake-" + secrets.token_urlsafe(16)
        self._access[token] = {
            "scopes": scopes,
            "client_id": client_id,
            "sub": self.user.sub,
            "exp": time.time() + 3600,
        }
        return token

    def expire_access_tokens(self) -> None:
        self._access.clear()

    def _authorized(self, request: Request) -> dict[str, Any] | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        grant = self._access.get(auth[7:])
        if grant is None or auth[7:] in self.revoked or grant["exp"] < time.time():
            return None
        return grant

    # --- OAuth endpoints -------------------------------------------------------------

    async def authorize(self, request: Request) -> Response:
        params = dict(request.query_params)
        self.authorize_requests.append(params)
        redirect = params["redirect_uri"]
        if self.consent_error:
            q = urlencode({"error": self.consent_error, "state": params.get("state", "")})
            return RedirectResponse(f"{redirect}?{q}", status_code=302)
        code = "code-" + secrets.token_urlsafe(16)
        self._codes[code] = params
        q = urlencode({"code": code, "state": params.get("state", "")})
        return RedirectResponse(f"{redirect}?{q}", status_code=302)

    def _granted(self, requested: str) -> list[str]:
        if self.grant_scopes is not None:
            return list(self.grant_scopes)
        scopes = []
        for s in requested.split():
            scopes.append(USERINFO_EMAIL if s == "email" else s)
        return scopes

    async def token(self, request: Request) -> Response:
        form = await request.form()
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("grant_type") == "authorization_code":
            params = self._codes.pop(str(form.get("code")), None)
            if params is None or params["redirect_uri"] != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            digest = hashlib.sha256(str(form.get("code_verifier")).encode()).digest()
            if base64.urlsafe_b64encode(digest).decode().rstrip("=") != params["code_challenge"]:
                return JSONResponse({"error": "invalid_grant", "d": "pkce"}, status_code=400)
            scopes = self._granted(params["scope"])
            access = self._issue_access(scopes, CLIENT_ID)
            body: dict[str, Any] = {
                "access_token": access,
                "expires_in": 3599,
                "token_type": "Bearer",
                "scope": " ".join(scopes),
                "id_token": self._id_token(params.get("nonce", ""), CLIENT_ID),
            }
            if params.get("access_type") == "offline":
                refresh = "1//fake-" + secrets.token_urlsafe(16)
                self._refresh[refresh] = {"scopes": scopes, "sub": self.user.sub}
                body["refresh_token"] = refresh
            return JSONResponse(body)
        if form.get("grant_type") == "refresh_token":
            refresh = str(form.get("refresh_token"))
            grant = self._refresh.get(refresh)
            if grant is None or refresh in self.revoked:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            access = self._issue_access(grant["scopes"], CLIENT_ID)
            return JSONResponse(
                {
                    "access_token": access,
                    "expires_in": 3599,
                    "token_type": "Bearer",
                    "scope": " ".join(grant["scopes"]),
                }
            )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def tokeninfo(self, request: Request) -> Response:
        form = await request.form()
        token = str(form.get("access_token"))
        grant = self._access.get(token)
        if grant is None or token in self.revoked:
            return JSONResponse({"error": "invalid_token"}, status_code=400)
        return JSONResponse(
            {
                "aud": grant["client_id"],
                "azp": grant["client_id"],
                "sub": grant["sub"],
                "scope": " ".join(grant["scopes"]),
                "expires_in": int(grant["exp"] - time.time()),
            }
        )

    async def revoke(self, request: Request) -> Response:
        form = await request.form()
        self.revoked.add(str(form.get("token")))
        return Response(status_code=200)

    async def certs(self, request: Request) -> Response:
        return JSONResponse(self.jwks())

    # --- Gmail API -------------------------------------------------------------------

    async def gmail(self, request: Request) -> Response:
        path = request.path_params["path"]
        self.gmail_requests.append((request.method, path, str(request.query_params)))
        self.gmail_authorizations.append(request.headers.get("authorization", ""))
        if request.method != "GET":
            self.write_attempts.append((request.method, path))
            return JSONResponse({"error": {"code": 405}}, status_code=405)
        grant = self._authorized(request)
        if grant is None:
            return JSONResponse({"error": {"code": 401}}, status_code=401)
        if GMAIL_READONLY not in grant["scopes"]:
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
            return JSONResponse(
                {
                    "threads": [{"id": t, "snippet": threads[t][-1]["snippet"]} for t in ids][
                        :max_results
                    ],
                    "resultSizeEstimate": len(ids),
                }
            )
        if len(parts) == 2 and parts[0] == "threads":
            if parts[1] not in threads:
                return JSONResponse({"error": {"code": 404}}, status_code=404)
            return JSONResponse({"id": parts[1], "messages": threads[parts[1]]})
        if len(parts) == 2 and parts[0] == "messages":
            for msgs in threads.values():
                for m in msgs:
                    if m["id"] == parts[1]:
                        return JSONResponse(m)
            return JSONResponse({"error": {"code": 404}}, status_code=404)
        if parts == ["labels"]:
            return JSONResponse(
                {
                    "labels": [
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                        {"id": "Label_1", "name": "Invoices", "type": "user"},
                    ]
                }
            )
        if parts == ["drafts"]:
            return JSONResponse({"drafts": [{"id": "d1", "message": {"id": "m9"}}]})
        if parts == ["drafts", "d1"]:
            draft = _message(
                "m9",
                "t9",
                subject="Draft reply",
                sender="pat@example.com",
                to="ana@vendor.test",
                date="Wed, 4 Nov 2026 12:00:00 +0000",
                labels=["DRAFT"],
                plain="Draft body",
            )
            return JSONResponse({"id": "d1", "message": draft})
        return JSONResponse({"error": {"code": 404}}, status_code=404)

    # --- app -------------------------------------------------------------------------

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/o/oauth2/v2/auth", self.authorize, methods=["GET"]),
                Route("/token", self.token, methods=["POST"]),
                Route("/tokeninfo", self.tokeninfo, methods=["POST"]),
                Route("/revoke", self.revoke, methods=["POST"]),
                Route("/oauth2/v3/certs", self.certs, methods=["GET"]),
                Route(
                    "/gmail/v1/users/me/{path:path}",
                    self.gmail,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                ),
            ]
        )


def endpoints_for(base_url: str) -> dict[str, Any]:
    """The ``identity.endpoints`` config block pointing at a fake served at ``base_url``."""
    return {
        "authorization_endpoint": f"{base_url}/o/oauth2/v2/auth",
        "token_endpoint": f"{base_url}/token",
        "revocation_endpoint": f"{base_url}/revoke",
        "tokeninfo_endpoint": f"{base_url}/tokeninfo",
        "jwks_uri": f"{base_url}/oauth2/v3/certs",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake Google OAuth + Gmail API for local runs")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--email", default="pat@example.com")
    parser.add_argument("--hd", default="example.com")
    args = parser.parse_args()
    fake = FakeGoogle(user=FakeUser(email=args.email, hd=args.hd))
    print(f"client_id={CLIENT_ID} client_secret={CLIENT_SECRET}")
    uvicorn.run(fake.app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
