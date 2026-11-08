from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from mcp_ro_proxy.auth.google import (
    GMAIL_READONLY,
    USERINFO_EMAIL,
    GoogleAccessTokens,
    GoogleOAuth,
    ReauthorizationRequiredError,
    ScopeError,
    SignInError,
    check_granted_scopes,
    pkce_pair,
)
from mcp_ro_proxy.crypto import TokenCipher, generate_key
from mcp_ro_proxy.store import SqlStore
from mcp_ro_proxy.upstreams.base import Principal
from tests.conftest import make_config
from tests.fakes.fake_google import FakeGoogle

FAKE = "http://127.0.0.1:9000"
READONLY = frozenset({GMAIL_READONLY})


@pytest.fixture
def fake() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
async def http(fake: FakeGoogle) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=fake.app()), base_url=FAKE
    ) as client:
        yield client


@pytest.fixture
def oauth(tmp_path: Path, http: httpx2.AsyncClient) -> GoogleOAuth:
    config = make_config(tmp_path, google_url=FAKE)
    return GoogleOAuth(
        config.identity,
        upstream_scopes=READONLY,
        redirect_uri="http://localhost:8000/auth/callback",
        http=http,
    )


async def sign_in(oauth: GoogleOAuth, http: httpx2.AsyncClient, *, nonce: str = "n1"):
    verifier, challenge = pkce_pair()
    url = oauth.authorization_url(state="st", nonce=nonce, code_challenge=challenge)
    resp = await http.get(url)
    code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
    return await oauth.complete_sign_in(code=code, code_verifier=verifier, nonce=nonce)


def test_authorization_url_requests_exactly_readonly(oauth: GoogleOAuth) -> None:
    url = oauth.authorization_url(state="s", nonce="n", code_challenge="c")
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert params["scope"] == f"openid email {GMAIL_READONLY}"
    assert params["access_type"] == "offline"
    assert params["include_granted_scopes"] == "false"
    assert params["code_challenge_method"] == "S256"
    assert params["hd"] == "example.com"
    assert params["prompt"] == "consent select_account"
    assert params["redirect_uri"] == "http://localhost:8000/auth/callback"


def test_identity_only_upstream_does_not_ask_for_offline(tmp_path: Path) -> None:
    config = make_config(tmp_path, google_url=FAKE)
    oauth = GoogleOAuth(
        config.identity, upstream_scopes=frozenset(), redirect_uri="x", http=httpx2.AsyncClient()
    )
    params = parse_qs(
        urlsplit(oauth.authorization_url(state="s", nonce="n", code_challenge="c")).query
    )
    assert params["scope"] == ["openid email"]
    assert "access_type" not in params


@pytest.mark.parametrize(
    ("granted", "reason"),
    [
        ({GMAIL_READONLY, "https://www.googleapis.com/auth/gmail.send"}, "write_scope_granted"),
        ({GMAIL_READONLY, "https://mail.google.com/"}, "write_scope_granted"),
        ({GMAIL_READONLY, "https://www.googleapis.com/auth/gmail.modify"}, "write_scope_granted"),
        ({GMAIL_READONLY, "https://www.googleapis.com/auth/gmail.compose"}, "write_scope_granted"),
        ({GMAIL_READONLY, "https://www.googleapis.com/auth/drive"}, "unexpected_scope_granted"),
        ({"openid", USERINFO_EMAIL}, "required_scope_missing"),
    ],
)
def test_granted_scope_check(granted: set[str], reason: str) -> None:
    with pytest.raises(ScopeError) as err:
        check_granted_scopes(frozenset(granted | {"openid"}), READONLY)
    assert err.value.reason == reason


def test_granted_scope_check_accepts_readonly_spellings() -> None:
    ok = frozenset({"openid", USERINFO_EMAIL, GMAIL_READONLY})
    assert check_granted_scopes(ok, READONLY) == ok
    assert check_granted_scopes(frozenset({"openid", "email", GMAIL_READONLY}), READONLY)


async def test_sign_in_happy_path(oauth: GoogleOAuth, http: httpx2.AsyncClient) -> None:
    result = await sign_in(oauth, http)
    assert result.sub == "1001"
    assert result.email == "pat@example.com"
    assert result.hd == "example.com"
    assert result.refresh_token
    assert GMAIL_READONLY in result.scopes


async def test_write_scope_grant_is_refused_and_revoked(
    oauth: GoogleOAuth, http: httpx2.AsyncClient, fake: FakeGoogle
) -> None:
    fake.grant_scopes = [
        "openid",
        USERINFO_EMAIL,
        GMAIL_READONLY,
        "https://www.googleapis.com/auth/gmail.send",
    ]
    with pytest.raises(ScopeError) as err:
        await sign_in(oauth, http)
    assert err.value.reason == "write_scope_granted"
    assert len(fake.revoked) == 1  # the refresh token was revoked at Google


async def test_unchecked_gmail_permission_is_refused(
    oauth: GoogleOAuth, http: httpx2.AsyncClient, fake: FakeGoogle
) -> None:
    fake.grant_scopes = ["openid", USERINFO_EMAIL]
    with pytest.raises(ScopeError) as err:
        await sign_in(oauth, http)
    assert err.value.reason == "required_scope_missing"


@pytest.mark.parametrize("hd", ["other.com", None])
async def test_wrong_or_missing_workspace_domain_is_refused(
    oauth: GoogleOAuth, http: httpx2.AsyncClient, fake: FakeGoogle, hd: str | None
) -> None:
    fake.user.hd = hd
    with pytest.raises(SignInError) as err:
        await sign_in(oauth, http)
    assert err.value.reason == "wrong_domain"
    assert fake.revoked


async def test_unverified_email_is_refused(
    oauth: GoogleOAuth, http: httpx2.AsyncClient, fake: FakeGoogle
) -> None:
    fake.user.email_verified = False
    with pytest.raises(SignInError) as err:
        await sign_in(oauth, http)
    assert err.value.reason == "email_unverified"


async def test_nonce_and_signature_are_verified(
    oauth: GoogleOAuth, http: httpx2.AsyncClient, fake: FakeGoogle
) -> None:
    verifier, challenge = pkce_pair()
    resp = await http.get(
        oauth.authorization_url(state="s", nonce="right", code_challenge=challenge)
    )
    code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
    with pytest.raises(SignInError, match="nonce"):
        await oauth.complete_sign_in(code=code, code_verifier=verifier, nonce="wrong")

    # A token signed by another key (same kid) fails verification.
    other = FakeGoogle()
    other._kid = fake._kid
    forged = other._id_token("n", "test-client.apps.googleusercontent.com")
    with pytest.raises(SignInError, match="failed verification"):
        await oauth.verify_id_token(forged, nonce="n")


async def test_bad_pkce_verifier_is_rejected(oauth: GoogleOAuth, http: httpx2.AsyncClient) -> None:
    _, challenge = pkce_pair()
    resp = await http.get(oauth.authorization_url(state="s", nonce="n", code_challenge=challenge))
    code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
    with pytest.raises(SignInError) as err:
        await oauth.complete_sign_in(code=code, code_verifier="wrong-verifier", nonce="n")
    assert err.value.reason == "code_exchange_failed"


async def test_access_tokens_refresh_cache_and_reauth(
    oauth: GoogleOAuth, http: httpx2.AsyncClient, fake: FakeGoogle, tmp_path: Path
) -> None:
    store = SqlStore(f"sqlite:///{tmp_path / 't.db'}", TokenCipher(generate_key("k")))
    tokens = GoogleAccessTokens(oauth, store, sign_in_url="http://localhost:8000")
    pat = Principal(sub="1001", email="pat@example.com", client="personal_key")
    with pytest.raises(ReauthorizationRequiredError, match="Sign in at"):
        await tokens.get(pat)

    result = await sign_in(oauth, http)
    await store.save_user(
        sub=result.sub,
        email=result.email,
        hd=result.hd,
        refresh_token=result.refresh_token,
        scopes=result.scopes,
    )
    first = await tokens.get(pat)
    assert await tokens.get(pat) == first  # cached
    tokens.invalidate("1001")
    assert await tokens.get(pat) != first

    fake.revoked.add(result.refresh_token or "")
    tokens.invalidate("1001")
    with pytest.raises(ReauthorizationRequiredError, match="Sign in again"):
        await tokens.get(pat)
    await store.aclose()
