"""Personal keys, for MCP clients that take a per-person API key instead of signing in with OAuth
(nexos.ai's custom MCP documents only that).

A person signs in with Google on /keys and gets a key, shown once. The guard keeps the key's hash
and that person's read-only Google refresh token (encrypted, in the same store as OAuth sign-ins).
On /mcp the key resolves to a fresh Google access token, which goes through the same checks as an
OAuth sign-in's: exactly the read-only scopes, the domain check, the allowlist. Each person has at
most one key; making a new one replaces it, and revoking it deletes the stored Google token too.

Every action on the page needs a fresh Google sign-in, so the page keeps no session of its own.
"""

import base64
import hashlib
import html
import logging
import secrets
import time
from typing import Any, cast
from urllib.parse import urlencode

from fastmcp.server.auth import AccessToken, MultiAuth, TokenVerifier
from key_value.aio.protocols import AsyncKeyValue
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_guard.config import IDENTITY_SCOPES
from mcp_guard.google import GoogleClient, ScopeLimitedTokenVerifier, in_domain, revoke

logger = logging.getLogger("mcp_guard")

KEY_PREFIX = "mcpg_"
KEY_LIFETIME_S = 180 * 24 * 3600
_LOGIN_TTL_S = 600
_STATE_COOKIE = "mcp_guard_keys_state"
_PEOPLE = "mcp_guard_people"
_KEYS = "mcp_guard_keys"
_LOGINS = "mcp_guard_key_logins"
# Refresh a cached Google access token this long before Google says it expires.
_EXPIRY_MARGIN_S = 120


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeyHeader:
    """Lets a client send its key in an X-API-Key header instead of Authorization: Bearer
    (whichever nexos uses). An explicit Authorization header always wins."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers: list[tuple[bytes, bytes]] = list(scope.get("headers", []))
            names = {name.lower() for name, _ in headers}
            key = next((v.strip() for n, v in headers if n.lower() == b"x-api-key"), b"")
            if key and b"authorization" not in names:
                scope = {**scope, "headers": [*headers, (b"authorization", b"Bearer " + key)]}
        await self.app(scope, receive, send)


class WithPersonalKeys(MultiAuth):
    """OAuth sign-in for Claude plus personal keys, on the same /mcp. The X-API-Key translation
    runs before FastMCP's authentication, which the app's own middleware would not."""

    def get_middleware(self) -> list[Any]:
        inherited = cast(list[Any], super().get_middleware())  # pyright: ignore[reportUnknownMemberType]
        return [Middleware(ApiKeyHeader), *inherited]


class PersonalKeys(TokenVerifier):
    """Verifies personal keys on /mcp, and serves the page that issues and revokes them."""

    def __init__(
        self,
        *,
        store: AsyncKeyValue,
        google: GoogleClient,
        verifier: ScopeLimitedTokenVerifier,
        base_url: str,
        upstream_scopes: list[str],
        allowed_domain: str,
        name: str,
    ):
        super().__init__()
        self._store = store
        self._google = google
        self._verifier = verifier
        self._base_url = base_url.rstrip("/")
        self._upstream_scopes = upstream_scopes
        self._domain = allowed_domain
        self._name = name
        # The page checks sign-ins itself and revokes any it refuses. Signing in to revoke a key
        # asks only who the person is, not for Gmail access.
        self._check_create = ScopeLimitedTokenVerifier(
            upstream_scopes,
            required=IDENTITY_SCOPES,
            client_id=google.client_id,
            endpoints=google.endpoints,
        )
        self._check_revoke = ScopeLimitedTokenVerifier(
            [], required=IDENTITY_SCOPES, client_id=google.client_id, endpoints=google.endpoints
        )
        self._access: dict[str, tuple[str, float]] = {}

    # --- on /mcp ------------------------------------------------------------------------------

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith(KEY_PREFIX):
            return None
        digest = _digest(token)
        entry = await self._store.get(digest, collection=_KEYS)
        person = entry and await self._store.get(str(entry["sub"]), collection=_PEOPLE)
        # The person record names their one current key, so a replaced key stops working even
        # if its own entry is still there.
        if not entry or not person or person.get("key_hash") != digest:
            return None
        access = await self._access_token(str(entry["sub"]), str(person["refresh_token"]))
        verified = access and await self._verifier.verify_token(access)
        if not verified:
            return None
        return verified.model_copy(update={"claims": {**verified.claims, "client": "personal_key"}})

    async def _access_token(self, sub: str, refresh_token: str) -> str | None:
        cached = self._access.get(sub)
        if cached and cached[1] > time.time():
            return cached[0]
        tokens = await self._google.refresh(refresh_token)
        if tokens is None:
            logger.warning("Couldn't refresh the Google token behind a personal key (sub=%s)", sub)
            return None
        self._access[sub] = (
            tokens.access_token,
            time.time() + tokens.expires_in - _EXPIRY_MARGIN_S,
        )
        return tokens.access_token

    # --- the page -----------------------------------------------------------------------------

    def routes(self) -> list[Route]:
        return [
            Route("/keys", self._page, methods=["GET"]),
            Route("/keys/login", self._login, methods=["GET"]),
            Route("/keys/callback", self._callback, methods=["GET"]),
        ]

    async def _page(self, request: Request) -> Response:
        return _html(
            self._name,
            f"""
<p>A personal key lets an MCP client that takes an API key, such as nexos.ai, read your Gmail
through this server, read-only. Claude signs in by itself and doesn't need one.</p>
<p>Each step asks you to sign in with Google ({html.escape(self._domain)} accounts only).</p>
<div class="row">
  <a class="button" href="/keys/login?action=create">Sign in and create a key</a>
  <a class="button secondary" href="/keys/login?action=revoke">Sign in and revoke my key</a>
</div>
<p class="muted">Creating a key replaces any key you made before.</p>""",
        )

    async def _login(self, request: Request) -> Response:
        action = request.query_params.get("action")
        if action not in ("create", "revoke"):
            return RedirectResponse("/keys", status_code=303)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        await self._store.put(
            state, {"action": action, "verifier": verifier}, collection=_LOGINS, ttl=_LOGIN_TTL_S
        )
        scopes = IDENTITY_SCOPES + (self._upstream_scopes if action == "create" else [])
        params = {
            "client_id": self._google.client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "hd": self._domain,
            "include_granted_scopes": "false",
            "prompt": "consent select_account",
        }
        if action == "create":
            params["access_type"] = "offline"
        response = RedirectResponse(f"{self._google.endpoints.authorize}?{urlencode(params)}", 303)
        # Ties the sign-in to this browser: a Google callback started elsewhere is refused.
        response.set_cookie(
            _STATE_COOKIE,
            state,
            max_age=_LOGIN_TTL_S,
            path="/keys",
            httponly=True,
            samesite="lax",
            secure=self._base_url.startswith("https://"),
        )
        return response

    @property
    def _redirect_uri(self) -> str:
        return f"{self._base_url}/keys/callback"

    async def _callback(self, request: Request) -> Response:  # noqa: PLR0911 - a return per refusal
        state = request.query_params.get("state", "")
        login = None
        if state and secrets.compare_digest(state, request.cookies.get(_STATE_COOKIE, "")):
            login = await self._store.get(state, collection=_LOGINS)
            await self._store.delete(state, collection=_LOGINS)
        if login is None:
            return self._fail("That sign-in expired or was started in another browser.", 400)
        code = request.query_params.get("code")
        if not code:
            return self._fail("Google sign-in was cancelled.", 400)
        tokens = await self._google.exchange(code, self._redirect_uri, str(login["verifier"]))
        if tokens is None:
            return self._fail("Google didn't complete the sign-in. Try again.", 502)
        create = login["action"] == "create"
        checker = self._check_create if create else self._check_revoke
        person = await checker.verify_token(tokens.access_token)
        if person is None:
            await revoke(self._google.endpoints, tokens.refresh_token or tokens.access_token)
            return self._fail(
                "Google didn't give exactly the read-only access this server allows.", 403
            )
        email = str(person.claims.get("email") or "")
        if not in_domain(person.claims, self._domain):
            await revoke(self._google.endpoints, tokens.refresh_token or tokens.access_token)
            logger.warning(
                "refused person=%s: not a verified %s account", email or "-", self._domain
            )
            return self._fail(f"Only {self._domain} accounts can use this server.", 403)
        sub = str(person.claims["sub"])
        if create:
            if not tokens.refresh_token:
                return self._fail("Google didn't grant offline access. Try again.", 502)
            return await self._create(sub, email, tokens.refresh_token)
        return await self._revoke(sub, email)

    async def _forget_key(self, sub: str) -> None:
        old = await self._store.get(sub, collection=_PEOPLE)
        if old and old.get("key_hash"):
            await self._store.delete(str(old["key_hash"]), collection=_KEYS)
        self._access.pop(sub, None)

    async def _create(self, sub: str, email: str, refresh_token: str) -> Response:
        await self._forget_key(sub)
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        digest = _digest(key)
        expires = int(time.time()) + KEY_LIFETIME_S
        record: dict[str, Any] = {
            "email": email,
            "refresh_token": refresh_token,
            "key_hash": digest,
        }
        await self._store.put(sub, record, collection=_PEOPLE, ttl=KEY_LIFETIME_S)
        await self._store.put(digest, {"sub": sub}, collection=_KEYS, ttl=KEY_LIFETIME_S)
        logger.info("personal key created person=%s", email)
        until = time.strftime("%Y-%m-%d", time.gmtime(expires))
        return _html(
            self._name,
            f"""
<p class="notice ok">Key created for {html.escape(email)}. Copy it now; it isn't shown again.</p>
<code class="secret">{key}</code>
<dl>
  <dt>MCP server URL</dt><dd><code>{html.escape(self._base_url)}/mcp</code></dd>
  <dt>Send it as</dt>
  <dd><code>Authorization: Bearer &lt;key&gt;</code> or <code>X-API-Key: &lt;key&gt;</code></dd>
  <dt>Valid until</dt><dd>{until}, or until you revoke it or make a new one</dd>
</dl>
<p><a href="/keys">Back</a></p>""",
        )

    async def _revoke(self, sub: str, email: str) -> Response:
        await self._forget_key(sub)
        # The stored Google token goes too. It isn't revoked at Google: that would also sign the
        # person out of Claude, which uses the same Google grant.
        await self._store.delete(sub, collection=_PEOPLE)
        logger.info("personal key revoked person=%s", email)
        return _html(
            self._name,
            f"""<p class="notice ok">Any key for {html.escape(email)} is revoked, and its Google
access is no longer stored here.</p><p><a href="/keys">Back</a></p>""",
        )

    def _fail(self, message: str, status: int) -> Response:
        return _html(
            self._name,
            f'<p class="notice warn">{html.escape(message)}</p><p><a href="/keys">Back</a></p>',
            status,
        )


_CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5b6475; --line:#e3e6ec;
  --accent:#1f5fd1; --on-accent:#fff; --ok:#ecf8f0; --ok-line:#9fd8b4; --warn:#fff6e5;
  --warn-line:#f1c87a; --code:#f1f3f7; color-scheme: light dark; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151b; --card:#1b2029; --text:#e7eaf0;
  --muted:#a2abbb; --line:#2c3340; --accent:#5b8ff0; --on-accent:#0d1118; --ok:#16281e;
  --ok-line:#2f6b47; --warn:#2b2414; --warn-line:#7a5f22; --code:#232a35; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width:640px; margin:40px auto; padding:24px 22px; background:var(--card);
  border:1px solid var(--line); border-radius:12px; }
@media (max-width:680px) { main { margin:16px; } }
h1 { font-size:22px; margin:0 0 16px; }
p { margin:0 0 14px; }
.muted { color:var(--muted); font-size:14px; }
.row { display:flex; gap:10px; flex-wrap:wrap; margin:0 0 14px; }
.button { display:inline-block; font-weight:600; border-radius:8px; padding:9px 16px;
  border:1px solid var(--accent); background:var(--accent); color:var(--on-accent);
  text-decoration:none; }
.button.secondary { background:transparent; color:var(--text); border-color:var(--line); }
.notice { border:1px solid; border-radius:10px; padding:12px 14px; }
.notice.ok { background:var(--ok); border-color:var(--ok-line); }
.notice.warn { background:var(--warn); border-color:var(--warn-line); }
code { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:13px;
  background:var(--code); border-radius:6px; padding:2px 6px; overflow-wrap:anywhere; }
code.secret { display:block; padding:10px 12px; margin:0 0 14px; user-select:all; }
dl { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:6px 14px; margin:0 0 14px; }
dt { color:var(--muted); } dd { margin:0; min-width:0; }
@media (max-width:480px) {
  dl { grid-template-columns:minmax(0,1fr); gap:2px; } dd { margin-bottom:8px; } }
a { color:var(--accent); }
"""


def _html(name: str, body: str, status: int = 200) -> HTMLResponse:
    title = f"{html.escape(name)} personal key"
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{title}</title><style>{_CSS}</style></head>
<body><main><h1>{title}</h1>{body}</main></body></html>""",
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )
