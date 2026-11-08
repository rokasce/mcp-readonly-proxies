"""scripts/playground_check.py, run against the fake Google instead of the real one."""

import pytest

from scripts.playground_check import check
from tests.conftest import Google, upstream
from tests.fakes.fake_google import GMAIL_READONLY, USERINFO_EMAIL


async def test_read_only_token_passes_every_check(
    google: Google, capsys: pytest.CaptureFixture[str]
) -> None:
    token = google.fake.issue([GMAIL_READONLY, USERINFO_EMAIL, "openid"])
    assert await check(
        token, "gmail", upstream(url=google.url), domain="example.com", endpoints=google.endpoints
    )
    out = capsys.readouterr().out
    assert "PASS  trash_thread refused" in out
    assert "Most recent threads (subjects only):" in out
    assert "FAIL" not in out
    assert google.fake.write_attempts == []


@pytest.mark.parametrize(
    "scopes",
    [
        [GMAIL_READONLY, USERINFO_EMAIL, "https://www.googleapis.com/auth/gmail.modify"],
        [USERINFO_EMAIL, "openid"],  # no Gmail access at all
    ],
)
async def test_token_the_guard_must_refuse(
    google: Google, capsys: pytest.CaptureFixture[str], scopes: list[str]
) -> None:
    assert await check(
        google.fake.issue(scopes), "gmail", upstream(url=google.url), endpoints=google.endpoints
    )
    assert "PASS  the guard refused the token (401)" in capsys.readouterr().out


async def test_expired_token_is_reported(
    google: Google, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not await check(
        "ya29.expired", "gmail", upstream(url=google.url), endpoints=google.endpoints
    )
    assert "copy a fresh one" in capsys.readouterr().out


async def test_outsider_is_reported(google: Google, capsys: pytest.CaptureFixture[str]) -> None:
    token = google.fake.issue([GMAIL_READONLY, USERINFO_EMAIL])
    assert not await check(
        token, "gmail", upstream(url=google.url), domain="other.example", endpoints=google.endpoints
    )
    assert "domain check refused" in capsys.readouterr().out
