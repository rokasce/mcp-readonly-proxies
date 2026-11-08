"""Google sign-in with the organization's own OAuth client, and per-user access tokens.

The proxy requests exactly ``openid email`` plus the scopes its upstream needs (for the
Gmail upstream: ``gmail.readonly``), with offline access so a refresh token comes back.
Every token Google returns is checked: the granted scope set must contain the required
scopes and nothing else, so a token that could write (``gmail.send``, ``gmail.compose``,
``gmail.modify``, ``https://mail.google.com/`` ...) is refused and revoked rather than
stored. The ID token is verified against Google's JWKS, and the account's ``hd`` claim
must equal the configured Workspace domain.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx2
import jwt

from mcp_ro_proxy.config import GoogleIdentityConfig
from mcp_ro_proxy.store import Store
from mcp_ro_proxy.upstreams.base import Principal, UpstreamError

OPENID = "openid"
EMAIL = "email"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

#: Google scopes that allow changing a mailbox or acting as the user. The allowlist check
#: already refuses anything unexpected; this list makes the refusal message explicit.
WRITE_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.settings.sharing",
        "https://www.googleapis.com/auth/gmail.addons.current.action.compose",
    }
)

#: Spellings Google may use in a granted scope string for what we asked as ``email``.
_IDENTITY_SCOPES = frozenset({OPENID, EMAIL, USERINFO_EMAIL})
_JWKS_TTL_SECONDS = 3600
_ACCESS_TOKEN_SKEW_SECONDS = 60


class SignInError(Exception):
    """Sign-in refused. ``reason`` is a stable code for the audit log."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class ScopeError(SignInError):
    pass


class ReauthorizationRequiredError(UpstreamError):
    """The stored Google grant can no longer be used; the user must sign in again."""


@dataclass(frozen=True)
class SignInResult:
    sub: str
    email: str
    hd: str
    refresh_token: str | None
    scopes: frozenset[str]


@dataclass(frozen=True)
class AccessGrant:
    access_token: str
    expires_at: float
    scopes: frozenset[str]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def check_granted_scopes(granted: frozenset[str], required: frozenset[str]) -> frozenset[str]:
    """Refuse a grant that is missing a required scope or holds any scope not requested."""
    unexpected = granted - required - _IDENTITY_SCOPES
    if unexpected:
        writes = sorted(unexpected & WRITE_SCOPES)
        detail = f"write scopes {writes}" if writes else f"unexpected scopes {sorted(unexpected)}"
        raise ScopeError(
            "write_scope_granted" if writes else "unexpected_scope_granted",
            f"Google granted {detail}; this proxy only accepts read-only access.",
        )
    missing = required - granted
    if missing:
        raise ScopeError(
            "required_scope_missing",
            "Google did not grant "
            + ", ".join(sorted(missing))
            + ". Sign in again and leave the requested permission checked.",
        )
    return granted


class GoogleOAuth:
    def __init__(
        self,
        config: GoogleIdentityConfig,
        *,
        upstream_scopes: frozenset[str],
        redirect_uri: str,
        http: httpx2.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._upstream_scopes = upstream_scopes
        self._redirect_uri = redirect_uri
        self._http = http
        self._clock = clock
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at = 0.0

    @property
    def requested_scopes(self) -> tuple[str, ...]:
        return (OPENID, EMAIL, *sorted(self._upstream_scopes))

    @property
    def required_scopes(self) -> frozenset[str]:
        return self._upstream_scopes

    @property
    def offline(self) -> bool:
        """Only upstreams that call Google on the user's behalf need a refresh token."""
        return bool(self._upstream_scopes)

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.requested_scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # A hint only; the hd claim is enforced after sign-in.
            "hd": self._config.workspace_domain,
            # Never merge in scopes granted to this client earlier.
            "include_granted_scopes": "false",
        }
        if self.offline:
            params["access_type"] = "offline"
            # Ask every time so Google returns a refresh token and shows the scopes.
            params["prompt"] = "consent select_account"
        else:
            params["prompt"] = "select_account"
        return f"{self._config.endpoints.authorization_endpoint}?{urlencode(params)}"

    async def _post_form(self, url: str, data: dict[str, str]) -> httpx2.Response:
        try:
            return await self._http.post(url, data=data, headers={"Accept": "application/json"})
        except httpx2.HTTPError as exc:
            raise SignInError("google_unreachable", "Could not reach Google.") from exc

    async def complete_sign_in(self, *, code: str, code_verifier: str, nonce: str) -> SignInResult:
        resp = await self._post_form(
            self._config.endpoints.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": self._redirect_uri,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret.get_secret_value(),
            },
        )
        if resp.status_code != 200:
            raise SignInError("code_exchange_failed", "Google rejected the sign-in code.")
        body = cast(dict[str, Any], resp.json())
        access_token = str(body.get("access_token") or "")
        refresh_token = body.get("refresh_token")
        id_token = str(body.get("id_token") or "")
        if not access_token or not id_token:
            raise SignInError("code_exchange_failed", "Google returned an incomplete token.")

        try:
            claims = await self.verify_id_token(id_token, nonce=nonce)
            granted = frozenset(str(body.get("scope", "")).split())
            check_granted_scopes(granted, self.required_scopes)
            # Defence in depth: ask Google what the access token can actually do.
            info_scopes = await self.tokeninfo_scopes(access_token)
            check_granted_scopes(info_scopes, self.required_scopes)
            if self.offline and not refresh_token:
                raise SignInError(
                    "no_refresh_token",
                    "Google did not return offline access. Sign in again and approve access.",
                )
        except SignInError:
            # Never keep a token we refuse: revoking it also drops the grant at Google.
            await self.revoke(str(refresh_token or access_token))
            raise

        return SignInResult(
            sub=str(claims["sub"]),
            email=str(claims["email"]).lower(),
            hd=str(claims["hd"]).lower(),
            refresh_token=str(refresh_token) if refresh_token else None,
            scopes=granted,
        )

    async def _get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        fresh = self._clock() - self._jwks_fetched_at < _JWKS_TTL_SECONDS
        if self._jwks is not None and fresh and not force:
            return self._jwks
        try:
            resp = await self._http.get(self._config.endpoints.jwks_uri)
            resp.raise_for_status()
        except httpx2.HTTPError as exc:
            raise SignInError("google_unreachable", "Could not fetch Google signing keys.") from exc
        self._jwks = cast(dict[str, Any], resp.json())
        self._jwks_fetched_at = self._clock()
        return self._jwks

    async def _signing_key(self, kid: str) -> Any:
        for force in (False, True):
            jwks = await self._get_jwks(force=force)
            for key in cast(list[dict[str, Any]], jwks.get("keys", [])):
                if key.get("kid") == kid:
                    return jwt.PyJWK.from_dict(key).key
        raise SignInError("id_token_invalid", "Google ID token was signed with an unknown key.")

    async def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SignInError("id_token_invalid", "Google ID token is malformed.") from exc
        key = await self._signing_key(str(header.get("kid", "")))
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=key,
                algorithms=["RS256"],
                audience=self._config.client_id,
                issuer=list(self._config.endpoints.issuers),
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise SignInError("id_token_invalid", "Google ID token failed verification.") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInError("id_token_invalid", "Google ID token nonce mismatch.")
        if claims.get("email_verified") is not True or not claims.get("email"):
            raise SignInError("email_unverified", "Your Google email address is not verified.")
        hd = str(claims.get("hd", "")).lower()
        if hd != self._config.workspace_domain:
            raise SignInError(
                "wrong_domain",
                f"Only {self._config.workspace_domain} Google Workspace accounts can sign in.",
            )
        return claims

    async def tokeninfo_scopes(self, access_token: str) -> frozenset[str]:
        resp = await self._post_form(
            self._config.endpoints.tokeninfo_endpoint, {"access_token": access_token}
        )
        if resp.status_code != 200:
            raise SignInError("tokeninfo_failed", "Google could not describe the access token.")
        info = cast(dict[str, Any], resp.json())
        audience = str(info.get("aud") or info.get("azp") or "")
        if audience != self._config.client_id:
            raise SignInError("tokeninfo_failed", "Access token was issued to another client.")
        return frozenset(str(info.get("scope", "")).split())

    async def refresh(self, refresh_token: str) -> AccessGrant:
        try:
            resp = await self._http.post(
                self._config.endpoints.token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret.get_secret_value(),
                },
                headers={"Accept": "application/json"},
            )
        except httpx2.HTTPError as exc:
            raise UpstreamError("Could not reach Google to refresh access.") from exc
        if resp.status_code in (400, 401):
            raise ReauthorizationRequiredError("Google access expired or was revoked.")
        if resp.status_code != 200:
            raise UpstreamError("Google token refresh failed; try again later.")
        body = cast(dict[str, Any], resp.json())
        granted = frozenset(str(body.get("scope", "")).split())
        try:
            check_granted_scopes(granted, self.required_scopes)
        except ScopeError as exc:
            raise ReauthorizationRequiredError(exc.message) from exc
        return AccessGrant(
            access_token=str(body["access_token"]),
            expires_at=self._clock() + float(body.get("expires_in", 3600)),
            scopes=granted,
        )

    async def revoke(self, token: str) -> None:
        if not token:
            return
        try:
            await self._http.post(self._config.endpoints.revocation_endpoint, data={"token": token})
        except httpx2.HTTPError:
            return


class GoogleAccessTokens:
    """Per-user short-lived Google access tokens, refreshed from the stored refresh token."""

    def __init__(
        self,
        oauth: GoogleOAuth,
        store: Store,
        *,
        sign_in_url: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oauth = oauth
        self._store = store
        self._sign_in_url = sign_in_url
        self._clock = clock
        self._cache: dict[str, AccessGrant] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def invalidate(self, sub: str) -> None:
        self._cache.pop(sub, None)

    async def get(self, principal: Principal) -> str:
        cached = self._cache.get(principal.sub)
        if cached and cached.expires_at - _ACCESS_TOKEN_SKEW_SECONDS > self._clock():
            return cached.access_token
        lock = self._locks.setdefault(principal.sub, asyncio.Lock())
        async with lock:
            cached = self._cache.get(principal.sub)
            if cached and cached.expires_at - _ACCESS_TOKEN_SKEW_SECONDS > self._clock():
                return cached.access_token
            user = await self._store.get_user(principal.sub)
            if user is None or not user.refresh_token:
                raise ReauthorizationRequiredError(
                    f"No Google access is stored for you. Sign in at {self._sign_in_url}"
                )
            try:
                grant = await self._oauth.refresh(user.refresh_token)
            except ReauthorizationRequiredError as exc:
                raise ReauthorizationRequiredError(
                    f"{exc} Sign in again at {self._sign_in_url}"
                ) from exc
            self._cache[principal.sub] = grant
            return grant.access_token
