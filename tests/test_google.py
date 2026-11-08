"""The token check every request goes through, against the fake Google over HTTP."""

import pytest

from mcp_guard.config import EMAIL_SCOPE, IDENTITY_SCOPES
from mcp_guard.google import ScopeLimitedTokenVerifier
from mcp_guard.server import dev_auth
from tests.conftest import Google, upstream
from tests.fakes.fake_google import CLIENT_ID, GMAIL_READONLY, FakeUser

READ_ONLY = ["openid", EMAIL_SCOPE, GMAIL_READONLY]


def deployed(google: Google) -> ScopeLimitedTokenVerifier:
    return ScopeLimitedTokenVerifier(
        [GMAIL_READONLY],
        required=IDENTITY_SCOPES,
        client_id=CLIENT_ID,
        revoke_refused=True,
        endpoints=google.endpoints,
    )


async def test_read_only_token_accepted_with_identity(google: Google) -> None:
    token = await deployed(google).verify_token(google.fake.issue(READ_ONLY))
    assert token is not None
    assert token.claims["sub"] == "1001"
    assert token.claims["email"] == "pat@example.com"
    assert token.claims["email_verified"] == "true"
    assert token.claims["google_user_data"]["hd"] == "example.com"


@pytest.mark.parametrize(
    "extra",
    [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
)
async def test_token_with_more_access_refused_and_revoked(google: Google, extra: str) -> None:
    token = google.fake.issue([*READ_ONLY, extra])
    assert await deployed(google).verify_token(token) is None
    # The guard obtained it, so the broader grant is dropped at Google too.
    assert google.fake.is_revoked(token)


async def test_token_without_gmail_access_refused(google: Google) -> None:
    token = google.fake.issue(IDENTITY_SCOPES)
    assert await deployed(google).verify_token(token) is None
    assert not google.fake.is_revoked(token)


async def test_token_for_another_client_refused(google: Google) -> None:
    token = google.fake.issue(READ_ONLY, client_id="someone-else.apps.googleusercontent.com")
    assert await deployed(google).verify_token(token) is None


async def test_unknown_token_refused(google: Google) -> None:
    assert await deployed(google).verify_token("ya29.made-up") is None


async def test_consumer_account_has_no_workspace_domain(google: Google) -> None:
    token = google.fake.issue(READ_ONLY, user=FakeUser(email="pat@example.com", hd=None))
    verified = await deployed(google).verify_token(token)
    assert verified is not None
    assert verified.claims["google_user_data"].get("hd") is None  # the domain check refuses it


async def test_revoked_grant_stops_working(google: Google) -> None:
    verifier = deployed(google)
    token = google.fake.issue(READ_ONLY)
    assert await verifier.verify_token(token) is not None
    google.fake.grant_for(token).revoked = True  # type: ignore[union-attr]
    # Trusted from the cache for at most a minute, then asked again.
    verifier._cache = type(verifier._cache)(ttl_seconds=60)  # pyright: ignore[reportPrivateUsage]
    assert await verifier.verify_token(token) is None


# Local test mode (--dev): a token the person pasted, e.g. from OAuth Playground.


async def test_dev_token_read_only_accepted(google: Google) -> None:
    token = google.fake.issue([EMAIL_SCOPE, GMAIL_READONLY], client_id="oauth-playground")
    verified = await dev_auth(upstream(), endpoints=google.endpoints).verify_token(token)
    assert verified is not None
    assert verified.claims["email"] == "pat@example.com"


async def test_dev_token_from_oauth_playground_accepted(google: Google) -> None:
    """Google's tokeninfo lists the short alias "email" next to userinfo.email."""
    token = google.fake.issue(["email", GMAIL_READONLY, EMAIL_SCOPE, "openid"])
    assert await dev_auth(upstream(), endpoints=google.endpoints).verify_token(token) is not None


@pytest.mark.parametrize(
    "extra", ["https://mail.google.com/", "https://www.googleapis.com/auth/gmail.modify"]
)
async def test_dev_token_with_more_access_refused_not_revoked(google: Google, extra: str) -> None:
    token = google.fake.issue([EMAIL_SCOPE, GMAIL_READONLY, extra])
    assert await dev_auth(upstream(), endpoints=google.endpoints).verify_token(token) is None
    # It's the person's own token, not one the guard obtained: refused, but left alone.
    assert not google.fake.is_revoked(token)
