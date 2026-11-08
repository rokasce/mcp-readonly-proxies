"""The proxy's small web UI: Google sign-in and personal key management.

Flow: ``/`` -> ``/auth/login`` (Google, PKCE + state + nonce) -> ``/auth/callback``
(verify, check scopes and domain, store the encrypted refresh token) -> ``/keys`` where
the user creates a personal key (shown once) or revokes keys. ``/disconnect`` deletes the
stored Google grant, revokes it at Google and revokes all of the user's keys.
"""

from __future__ import annotations

import html
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_ro_proxy.audit import AuditLogger
from mcp_ro_proxy.auth.google import GoogleAccessTokens, GoogleOAuth, SignInError, pkce_pair
from mcp_ro_proxy.config import ProxyConfig
from mcp_ro_proxy.store import ApiKeyRecord, Store, TooManyKeysError
from mcp_ro_proxy.upstreams.base import Principal

_SESSION_LOGIN = "login"
_SESSION_USER = "user"
_SESSION_CSRF = "csrf"
_LOGIN_MAX_AGE_SECONDS = 600


@dataclass(frozen=True)
class SessionUser:
    sub: str
    email: str


def _fmt_time(epoch: int | None) -> str:
    if epoch is None:
        return "never"
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")


_CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #1d2330; --muted: #5b6475; --line: #e3e6ec;
  --accent: #1f5fd1; --accent-text: #ffffff; --danger: #b42318; --ok-bg: #ecf8f0;
  --ok-line: #9fd8b4; --warn-bg: #fff6e5; --warn-line: #f1c87a; --code-bg: #f1f3f7;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --card: #1b2029; --text: #e7eaf0; --muted: #a2abbb; --line: #2c3340;
    --accent: #5b8ff0; --accent-text: #0d1118; --danger: #f07a6f; --ok-bg: #16281e;
    --ok-line: #2f6b47; --warn-bg: #2b2414; --warn-line: #7a5f22; --code-bg: #232a35;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 720px; margin: 0 auto; padding: 40px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 17px; margin: 0 0 12px; }
p { margin: 0 0 12px; }
.muted { color: var(--muted); }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 20px 22px; margin-top: 20px;
}
.badge {
  display: inline-block; font-size: 12px; font-weight: 600; padding: 2px 8px;
  border-radius: 999px; border: 1px solid var(--line); color: var(--muted);
}
.badge.ok { background: var(--ok-bg); border-color: var(--ok-line); color: var(--text); }
.notice { border-radius: 10px; padding: 12px 14px; margin-top: 16px; border: 1px solid; }
.notice.ok { background: var(--ok-bg); border-color: var(--ok-line); }
.notice.warn { background: var(--warn-bg); border-color: var(--warn-line); }
.button, button {
  display: inline-block; font: inherit; font-weight: 600; cursor: pointer;
  border-radius: 8px; padding: 9px 16px; border: 1px solid var(--accent);
  background: var(--accent); color: var(--accent-text); text-decoration: none;
}
button.secondary, .button.secondary {
  background: transparent; color: var(--text); border-color: var(--line);
}
button.danger { background: transparent; color: var(--danger); border-color: var(--line); }
button.small { padding: 4px 10px; font-size: 13px; }
form.inline { display: inline; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
input[type=text] {
  font: inherit; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--card); color: var(--text); flex: 1 1 220px; min-width: 0;
}
code, .secret {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px;
  background: var(--code-bg); border-radius: 6px; padding: 2px 6px; word-break: break-all;
}
.secret { display: block; padding: 10px 12px; margin: 8px 0; user-select: all; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 14px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td {
  text-align: left; padding: 8px 6px; border-top: 1px solid var(--line); vertical-align: top;
}
th { color: var(--muted); font-weight: 600; border-top: none; }
td.actions { text-align: right; white-space: nowrap; }
.table-wrap { overflow-x: auto; }
footer { margin-top: 28px; font-size: 13px; }
@media (max-width: 560px) {
  th.when, td.when { display: none; }
}
"""


class WebUI:
    def __init__(
        self,
        config: ProxyConfig,
        *,
        oauth: GoogleOAuth,
        store: Store,
        audit: AuditLogger,
        tokens: GoogleAccessTokens | None,
    ) -> None:
        self._config = config
        self._oauth = oauth
        self._store = store
        self._audit = audit
        self._tokens = tokens
        self._title = f"{config.upstream.name.capitalize()} read-only connector"
        self._mcp_url = f"{config.server.public_base_url}{config.server.mcp_path}"
        self._is_gmail = config.upstream.kind == "native" and config.upstream.provider == "gmail"
        self._footer = (
            "Read-only by design: this connector can search and read mail, and cannot send, "
            "draft, label or delete."
            if self._is_gmail
            else "Read-only by design: only reviewed, read-only tools are exposed."
        )

    def routes(self) -> list[Route]:
        return [
            Route("/", self._guard(self.index), methods=["GET"]),
            Route("/auth/login", self._guard(self.login), methods=["GET"]),
            Route("/auth/callback", self._guard(self.callback), methods=["GET"]),
            Route("/keys", self._guard(self.keys), methods=["GET"]),
            Route("/keys", self._guard(self.create_key), methods=["POST"]),
            Route("/keys/{key_id}/revoke", self._guard(self.revoke_key), methods=["POST"]),
            Route("/disconnect", self._guard(self.disconnect), methods=["POST"]),
            Route("/logout", self._guard(self.logout), methods=["POST"]),
            Route("/healthz", self.healthz, methods=["GET"]),
        ]

    # --- helpers ---------------------------------------------------------------------

    @staticmethod
    def _guard(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            return response

        return wrapped

    @staticmethod
    def _session(request: Request) -> dict[str, Any]:
        return request.session

    def _csrf(self, request: Request) -> str:
        session = self._session(request)
        token = session.get(_SESSION_CSRF)
        if not isinstance(token, str):
            token = secrets.token_urlsafe(32)
            session[_SESSION_CSRF] = token
        return token

    def _csrf_ok(self, request: Request, form: FormData) -> bool:
        expected = self._session(request).get(_SESSION_CSRF)
        supplied = form.get("csrf")
        return (
            isinstance(expected, str)
            and isinstance(supplied, str)
            and secrets.compare_digest(expected, supplied)
        )

    async def _user(self, request: Request) -> SessionUser | None:
        raw = self._session(request).get(_SESSION_USER)
        if not isinstance(raw, dict):
            return None
        sub, email = raw.get("sub"), raw.get("email")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(sub, str) or not isinstance(email, str):
            return None
        if await self._store.get_user(sub) is None:
            # The grant was deleted (disconnect elsewhere): the session is stale.
            self._session(request).pop(_SESSION_USER, None)
            return None
        return SessionUser(sub=sub, email=email)

    def _page(self, body: str, *, status: int = 200, script: str = "") -> HTMLResponse:
        nonce = secrets.token_urlsafe(16)
        script_tag = f'<script nonce="{nonce}">{script}</script>' if script else ""
        page = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(self._title)}</title><style>{_CSS}</style></head>"
            f"<body><main>{body}<footer class='muted'>{html.escape(self._footer)}</footer>"
            f"</main>{script_tag}</body></html>"
        )
        response = HTMLResponse(page, status_code=status)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'nonce-{nonce}'; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'"
        )
        return response

    def _header(self, user: SessionUser | None) -> str:
        who = (
            f"<p class='muted'>Signed in as <strong>{html.escape(user.email)}</strong></p>"
            if user
            else f"<p class='muted'>For {html.escape(self._config.identity.workspace_domain)}"
            " Google Workspace accounts</p>"
        )
        return f"<h1>{html.escape(self._title)}</h1>{who}"

    def _error(self, message: str, *, status: int) -> HTMLResponse:
        body = (
            f"{self._header(None)}<div class='card'><h2>Sign-in failed</h2>"
            f"<p>{html.escape(message)}</p>"
            "<a class='button' href='/auth/login'>Try again</a></div>"
        )
        return self._page(body, status=status)

    # --- pages -----------------------------------------------------------------------

    async def healthz(self, request: Request) -> Response:
        return PlainTextResponse("ok")

    async def index(self, request: Request) -> Response:
        user = await self._user(request)
        if user is not None:
            return RedirectResponse("/keys", status_code=303)
        notice = ""
        if request.query_params.get("disconnected"):
            notice = (
                "<div class='notice ok'>Disconnected. Your stored Google access was deleted "
                "and revoked, and all your personal keys were revoked.</div>"
            )
        body = (
            f"{self._header(None)}{notice}<div class='card'>"
            f"{self._intro()}"
            "<p>After signing in you can create a personal key for nexos.ai.</p>"
            "<a class='button' href='/auth/login'>Sign in with Google</a></div>"
        )
        return self._page(body)

    def _intro(self) -> str:
        if self._is_gmail:
            return (
                "<h2>Connect your mailbox, read-only</h2>"
                "<p>Sign in with your Google Workspace account to give this connector "
                "read-only access to your Gmail. Google is asked only for "
                "<code>gmail.readonly</code>, so even a bug could not send, change or delete "
                "mail.</p>"
            )
        return (
            "<h2>Sign in</h2><p>Sign in with your Google Workspace account to use this "
            "read-only connector.</p>"
        )

    async def login(self, request: Request) -> Response:
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._session(request)[_SESSION_LOGIN] = {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "started": int(datetime.now(UTC).timestamp()),
        }
        url = self._oauth.authorization_url(state=state, nonce=nonce, code_challenge=challenge)
        return RedirectResponse(url, status_code=302)

    def _callback_problem(self, login: object, params: Mapping[str, str]) -> Response | None:
        """Validate the callback against the login this browser started."""
        if not isinstance(login, dict):
            return self._error("Your sign-in session expired. Start again.", status=400)
        state = str(login.get("state", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        started = int(login.get("started", 0))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if not secrets.compare_digest(state, params.get("state", "")):
            return self._error("Sign-in state did not match. Start again.", status=400)
        if datetime.now(UTC).timestamp() - started > _LOGIN_MAX_AGE_SECONDS:
            return self._error("Your sign-in session expired. Start again.", status=400)
        if params.get("error"):
            self._audit.emit(
                "sign_in", severity="WARNING", decision="deny", reason="consent_denied"
            )
            return self._error("Google sign-in was cancelled or denied.", status=400)
        if not params.get("code"):
            return self._error("Google did not return a sign-in code.", status=400)
        return None

    async def callback(self, request: Request) -> Response:
        login = self._session(request).pop(_SESSION_LOGIN, None)
        params = request.query_params
        problem = self._callback_problem(login, params)
        if problem is not None:
            return problem
        assert isinstance(login, dict)  # noqa: S101 - narrowed by _callback_problem
        try:
            result = await self._oauth.complete_sign_in(
                code=params["code"],
                code_verifier=str(login.get("verifier", "")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                nonce=str(login.get("nonce", "")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )
        except SignInError as exc:
            self._audit.emit("sign_in", severity="WARNING", decision="deny", reason=exc.reason)
            return self._error(exc.message, status=403)

        await self._store.save_user(
            sub=result.sub,
            email=result.email,
            hd=result.hd,
            refresh_token=result.refresh_token,
            scopes=result.scopes,
        )
        if self._tokens is not None:
            self._tokens.invalidate(result.sub)
        principal = Principal(sub=result.sub, email=result.email, client="web")
        self._audit.emit(
            "sign_in",
            principal=principal,
            decision="allow",
            reason="ok",
            scopes=sorted(result.scopes),
        )
        session = self._session(request)
        session[_SESSION_USER] = {"sub": result.sub, "email": result.email}
        session[_SESSION_CSRF] = secrets.token_urlsafe(32)
        return RedirectResponse("/keys", status_code=303)

    def _key_rows(self, keys: list[ApiKeyRecord], csrf: str) -> str:
        now = int(datetime.now(UTC).timestamp())
        rows: list[str] = []
        for key in keys:
            if key.revoked_at is not None:
                status = "<span class='badge'>revoked</span>"
            elif not key.is_active(now):
                status = "<span class='badge'>expired</span>"
            else:
                status = "<span class='badge ok'>active</span>"
            action = (
                f"<form class='inline' method='post' action='/keys/{html.escape(key.id)}/revoke'>"
                f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
                "<button class='danger small' type='submit'>Revoke</button></form>"
                if key.is_active(now)
                else ""
            )
            rows.append(
                "<tr>"
                f"<td>{html.escape(key.label)}<br><code>{html.escape(key.hint)}…</code></td>"
                f"<td>{status}</td>"
                f"<td class='when'>Created {_fmt_time(key.created_at)}<br>"
                f"Last used {_fmt_time(key.last_used_at)}<br>"
                f"Expires {_fmt_time(key.expires_at)}</td>"
                f"<td class='actions'>{action}</td></tr>"
            )
        if not rows:
            return "<p class='muted'>You have no personal keys yet.</p>"
        return (
            "<div class='table-wrap'><table><thead><tr><th>Key</th><th>Status</th>"
            "<th class='when'>Dates</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

    async def _keys_page(
        self,
        request: Request,
        user: SessionUser,
        *,
        new_key: str | None = None,
        notice: str = "",
        status: int = 200,
    ) -> Response:
        csrf = self._csrf(request)
        keys = list(await self._store.list_keys(user.sub))
        script = ""
        created = ""
        if new_key is not None:
            created = (
                "<div class='notice ok'><strong>Your new personal key</strong> "
                "(shown only once, copy it now):"
                f"<span class='secret' id='new-key'>{html.escape(new_key)}</span>"
                "<button class='secondary small' type='button' id='copy'>Copy key</button>"
                "</div>"
            )
            script = (
                "document.getElementById('copy').addEventListener('click',function(){"
                "navigator.clipboard.writeText(document.getElementById('new-key').textContent)"
                ".then(()=>{this.textContent='Copied'});});"
            )
        ttl = self._config.client_auth.key_ttl_days
        ttl_text = f" Keys expire after {ttl} days." if ttl else ""
        body = (
            f"{self._header(user)}{notice}{created}"
            "<div class='card'><h2>Use it in nexos.ai</h2><dl>"
            f"<dt>Server URL</dt><dd><code>{html.escape(self._mcp_url)}</code></dd>"
            "<dt>Authentication</dt><dd>Your personal key, sent as "
            "<code>Authorization: Bearer &lt;key&gt;</code> or <code>X-API-Key</code></dd>"
            "</dl></div>"
            "<div class='card'><h2>Personal keys</h2>"
            "<form method='post' action='/keys' class='row'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
            "<input type='text' name='label' maxlength='100' placeholder='Label, e.g. nexos.ai' "
            "aria-label='Key label'>"
            "<button type='submit'>Create key</button></form>"
            f"<p class='muted' style='margin-top:10px'>Anyone holding a key can read your "
            f"mail through this connector. Revoke keys you no longer use.{ttl_text}</p>"
            f"{self._key_rows(keys, csrf)}</div>"
            "<div class='card'><h2>Account</h2><div class='row'>"
            "<form class='inline' method='post' action='/logout'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
            "<button class='secondary' type='submit'>Sign out</button></form>"
            "<form class='inline' method='post' action='/disconnect'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
            "<button class='danger' type='submit'>Disconnect and revoke everything</button>"
            "</form></div><p class='muted' style='margin-top:10px'>Disconnect deletes the "
            "stored Google access, revokes it at Google and revokes all your keys.</p></div>"
        )
        return self._page(body, status=status, script=script)

    async def keys(self, request: Request) -> Response:
        user = await self._user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        return await self._keys_page(request, user)

    async def create_key(self, request: Request) -> Response:
        user = await self._user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        if not self._csrf_ok(request, form):
            return PlainTextResponse("Invalid form token. Reload the page.", status_code=403)
        label = str(form.get("label") or "").strip() or "personal key"
        ttl_days = self._config.client_auth.key_ttl_days
        try:
            record, raw = await self._store.create_api_key(
                sub=user.sub,
                label=label,
                ttl_seconds=ttl_days * 86_400 if ttl_days else None,
                max_active=self._config.client_auth.max_active_keys_per_user,
            )
        except TooManyKeysError as exc:
            notice = f"<div class='notice warn'>{html.escape(str(exc))}. Revoke one first.</div>"
            return await self._keys_page(request, user, notice=notice, status=409)
        self._audit.emit(
            "key_created",
            principal=Principal(user.sub, user.email, "web", record.id),
            decision="allow",
            reason="ok",
        )
        return await self._keys_page(request, user, new_key=raw)

    async def revoke_key(self, request: Request) -> Response:
        user = await self._user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        if not self._csrf_ok(request, form):
            return PlainTextResponse("Invalid form token. Reload the page.", status_code=403)
        key_id = request.path_params["key_id"]
        if await self._store.revoke_key(user.sub, key_id):
            self._audit.emit(
                "key_revoked",
                principal=Principal(user.sub, user.email, "web", key_id),
                decision="allow",
                reason="ok",
            )
        return RedirectResponse("/keys", status_code=303)

    async def disconnect(self, request: Request) -> Response:
        user = await self._user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        if not self._csrf_ok(request, form):
            return PlainTextResponse("Invalid form token. Reload the page.", status_code=403)
        deleted = await self._store.delete_user(user.sub)
        if deleted is not None and deleted.refresh_token:
            await self._oauth.revoke(deleted.refresh_token)
        if self._tokens is not None:
            self._tokens.invalidate(user.sub)
        self._audit.emit(
            "disconnect",
            principal=Principal(user.sub, user.email, "web"),
            decision="allow",
            reason="ok",
        )
        self._session(request).clear()
        return RedirectResponse("/?disconnected=1", status_code=303)

    async def logout(self, request: Request) -> Response:
        form = await request.form()
        if self._csrf_ok(request, form):
            self._session(request).clear()
        return RedirectResponse("/", status_code=303)
