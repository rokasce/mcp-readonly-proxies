"""Google sign-in for the guard, and the check every Google token goes through.

Every request's Google token is checked with Google (tokeninfo): it must have been issued to the
guard's own OAuth client, hold the identity scopes and the upstream's read-only scopes, and
nothing else. A token with more access (say `https://mail.google.com/` or `gmail.modify`) is
refused, and when the guard itself obtained it (deployed mode) it is also revoked at Google so the
broader grant doesn't linger.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx2
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.utilities.token_cache import TokenCache
from key_value.aio.protocols import AsyncKeyValue
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_guard.config import IDENTITY_SCOPES

logger = logging.getLogger("mcp_guard")

HTTP_TIMEOUT_S = 10
# How long a token Google confirmed is trusted before asking again. Short, so a token revoked at
# Google stops working within a minute, but long enough that an agent's burst of calls doesn't
# cost two Google round trips each.
VERIFY_CACHE_S = 60


@dataclass(frozen=True)
class GoogleEndpoints:
    authorize: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
    revoke: str = "https://oauth2.googleapis.com/revoke"
    tokeninfo: str = "https://oauth2.googleapis.com/tokeninfo"
    userinfo: str = "https://www.googleapis.com/oauth2/v2/userinfo"


GOOGLE = GoogleEndpoints()


async def revoke(endpoints: GoogleEndpoints, token: str) -> None:
    """Revoke a Google token; this drops the whole grant (and its refresh token) at Google."""
    try:
        async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
            await http.post(endpoints.revoke, data={"token": token})
    except httpx2.HTTPError as exc:
        logger.warning("Couldn't revoke a refused Google token: %s", exc)


def in_domain(claims: dict[str, Any], domain: str) -> bool:
    """Whether a checked token belongs to a verified Google Workspace account in `domain`.

    The Workspace domain (`hd`) must equal `domain`, not just the email's suffix: anyone can make
    a consumer Google account for an address like pat@example.com and verify it, and such an
    account has no `hd` and isn't governed by the company's Workspace (its admin can't suspend
    it, offboarding doesn't touch it). Requiring `hd` limits the guard to company-managed accounts.
    """
    email = str(claims.get("email") or "").lower()
    user_data = cast(dict[str, Any], claims.get("google_user_data") or {})
    hd = str(user_data.get("hd") or "").lower()
    # Google's tokeninfo sends email_verified as the string "true"/"false", userinfo as a bool;
    # anything else, including a missing claim, counts as unverified.
    verified = str(claims.get("email_verified")).lower() == "true"
    return email.endswith("@" + domain) and verified and hd == domain


class ScopeLimitedTokenVerifier(TokenVerifier):
    """Checks a Google access token with Google and refuses it unless it holds exactly the
    identity scopes plus the upstream's read-only scopes.

    Used for every request: in deployed mode on the token the sign-in obtained (and refreshes),
    in local test mode (--dev) on the token the person pasted in. `revoke_refused` revokes a
    refused token at Google; only for tokens the guard obtained, not one a person brought along.
    """

    def __init__(
        self,
        upstream_scopes: list[str],
        *,
        required: list[str],
        client_id: str | None = None,
        revoke_refused: bool = False,
        client: str = "oauth",
        endpoints: GoogleEndpoints = GOOGLE,
    ):
        super().__init__(required_scopes=[*required, *upstream_scopes])
        # Google's tokeninfo also lists "email" (short for userinfo.email) when that is granted.
        self._allowed = {*IDENTITY_SCOPES, "email", *upstream_scopes}
        self._client_id = client_id
        self._revoke_refused = revoke_refused
        # How the token came in, for the audit line: "oauth" (the guard's sign-in) or "dev".
        self._client = client
        self._endpoints = endpoints
        self._cache = TokenCache(ttl_seconds=VERIFY_CACHE_S)

    async def verify_token(self, token: str) -> AccessToken | None:
        cached, result = self._cache.get(token)
        if cached:
            return result
        try:
            async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
                bearer = {"Authorization": f"Bearer {token}"}
                info = await http.get(self._endpoints.tokeninfo, headers=bearer)
                if info.status_code != 200:
                    return None
                data = cast(dict[str, Any], info.json())
                verified = await self._check(token, data)
                if verified is None:
                    return None
                # The Workspace domain (hd) only comes from userinfo.
                user = await http.get(self._endpoints.userinfo, headers=bearer)
                user_data = cast(dict[str, Any], user.json()) if user.status_code == 200 else {}
        except httpx2.HTTPError as exc:
            logger.warning("Couldn't check a token with Google: %s", exc)
            return None
        claims = verified.claims
        claims["email"] = claims.get("email") or user_data.get("email")
        claims["email_verified"] = claims.get("email_verified") or user_data.get("verified_email")
        verified.claims["google_user_data"] = user_data or None
        self._cache.set(token, verified)
        return verified

    async def _check(self, token: str, data: dict[str, Any]) -> AccessToken | None:
        sub = str(data.get("sub") or "")
        if not sub or (self._client_id and data.get("aud") != self._client_id):
            logger.warning("Refused a Google token issued to another client or without a subject")
            return None
        scopes = str(data.get("scope") or "").split()
        missing = set(self.required_scopes) - set(scopes)
        if missing:
            logger.warning("Refused a Google token without %s", ", ".join(sorted(missing)))
            return None
        extra = set(scopes) - self._allowed
        if extra:
            logger.warning(
                "Refused a token with more access than allowed: %s", ", ".join(sorted(extra))
            )
            if self._revoke_refused:
                await revoke(self._endpoints, token)
            return None
        expires_in = str(data.get("expires_in") or "")
        return AccessToken(
            token=token,
            client_id=sub,
            scopes=scopes,
            expires_at=int(expires_in) + int(time.time()) if expires_in.isdigit() else None,
            subject=sub,
            claims={
                "sub": sub,
                "email": data.get("email"),
                "email_verified": data.get("email_verified"),
                "client": self._client,
            },
        )


class GoogleSignIn(OAuthProxy):
    """FastMCP's OAuth proxy to Google, as GoogleProvider sets it up, with two changes: the
    Google endpoints can be pointed elsewhere (the local fake; GoogleProvider hard-codes them),
    and a sign-in whose token would be refused fails at the token exchange, with the token
    revoked, rather than handing the client a token every request then rejects."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str,
        verifier: "ScopeLimitedTokenVerifier",
        allowed_domain: str,
        client_redirect_uris: list[str],
        client_storage: AsyncKeyValue,
        jwt_signing_key: str | None = None,
        endpoints: GoogleEndpoints = GOOGLE,
    ):
        super().__init__(
            upstream_authorization_endpoint=endpoints.authorize,
            upstream_token_endpoint=endpoints.token,
            upstream_revocation_endpoint=endpoints.revoke,
            upstream_client_id=client_id,
            upstream_client_secret=client_secret,
            token_verifier=verifier,
            base_url=base_url,
            # Only Claude's own callbacks; anything else is refused when the client registers.
            allowed_client_redirect_uris=client_redirect_uris,
            client_storage=client_storage,
            jwt_signing_key=jwt_signing_key,
            # The guard's "Claude wants to access <guard>" page shows the first time, then is
            # remembered per client and browser; cross-site navigations are still asked, which
            # blocks a malicious site from riding on an earlier approval.
            require_authorization_consent="remember",
            extra_authorize_params={
                # A refresh token, so the guard keeps working after the first hour.
                "access_type": "offline",
                "prompt": "consent",
                # Never fold in scopes this client was granted before.
                "include_granted_scopes": "false",
                # Preselects the company account; the domain is enforced after sign-in.
                "hd": allowed_domain,
            },
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        token = await super().exchange_authorization_code(client, authorization_code)
        # Checks (and on too much access revokes) the Google token just obtained, the same way
        # every later request will.
        if await self.load_access_token(token.access_token) is None:
            raise TokenError(
                "invalid_grant",
                "Google's sign-in didn't give exactly the read-only access this server allows.",
            )
        return token


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str
    expires_in: int
    scopes: list[str]
    refresh_token: str | None


class GoogleClient:
    """The few direct calls to Google's token endpoint the personal-key page needs."""

    def __init__(self, client_id: str, client_secret: str, endpoints: GoogleEndpoints = GOOGLE):
        self.client_id = client_id
        self._client_secret = client_secret
        self.endpoints = endpoints

    async def _token(self, form: dict[str, str]) -> GoogleTokens | None:
        form = {**form, "client_id": self.client_id, "client_secret": self._client_secret}
        try:
            async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
                response = await http.post(self.endpoints.token, data=form)
        except httpx2.HTTPError as exc:
            logger.warning("Couldn't reach Google's token endpoint: %s", exc)
            return None
        if response.status_code != 200:
            return None
        body = cast(dict[str, Any], response.json())
        return GoogleTokens(
            access_token=str(body["access_token"]),
            expires_in=int(body.get("expires_in") or 3600),
            scopes=str(body.get("scope") or "").split(),
            refresh_token=body.get("refresh_token"),
        )

    async def exchange(
        self, code: str, redirect_uri: str, code_verifier: str
    ) -> GoogleTokens | None:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> GoogleTokens | None:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
