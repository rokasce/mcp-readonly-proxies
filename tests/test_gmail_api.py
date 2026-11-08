"""The gmail_api upstream's tools, through the guard, against the fake Gmail API over HTTP."""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

from mcp_guard.gmail_api import html_to_text
from mcp_guard.server import create_guard
from tests.conftest import Google, upstream
from tests.fakes.fake_google import GMAIL_READONLY


def _as(monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    access = AccessToken(token=token, client_id="c", scopes=[]) if token else None
    monkeypatch.setattr("mcp_guard.gmail_api.get_access_token", lambda: access)


@pytest.fixture
async def client(google: Google, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Client[Any]]:
    _as(monkeypatch, google.fake.issue([GMAIL_READONLY]))
    async with Client(create_guard("gmail", upstream("gmail", url=google.url))) as c:
        yield c


async def call(client: Client[Any], name: str, args: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(name, args or {})
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_search_threads(client: Client[Any], google: Google) -> None:
    data = await call(client, "search_threads", {"query": "invoice", "max_results": 5})
    assert [t["id"] for t in data["threads"]] == ["t1"]
    thread = data["threads"][0]
    assert thread["subject"] == "Q3 invoice"
    assert thread["from"] == "Ana <ana@vendor.test>"
    assert thread["message_count"] == 2
    assert thread["last_message_date"] == "Mon, 2 Nov 2026 10:30:00 +0000"
    assert "INBOX" in thread["label_ids"]
    method, path, query = google.fake.gmail_requests[0]
    assert (method, path) == ("GET", "threads")
    assert "q=invoice" in query
    assert "maxResults=5" in query


async def test_get_thread_and_message_bodies(client: Client[Any]) -> None:
    thread = await call(client, "get_thread", {"thread_id": "t1"})
    first, reply = thread["messages"]
    assert first["body"] == {
        "text": "Please find the Q3 invoice attached.",
        "source": "text/plain",
        "truncated": False,
    }
    assert first["attachments"] == [
        {"filename": "invoice-q3.pdf", "mime_type": "application/pdf", "size": 12345}
    ]
    assert reply["body"]["text"] == "Thanks, received."
    assert reply["body"]["source"] == "text/html"

    headers_only = await call(client, "get_thread", {"thread_id": "t1", "include_bodies": False})
    assert "body" not in headers_only["messages"][0]

    message = await call(client, "get_message", {"message_id": "m3", "max_body_chars": 7})
    assert message["subject"] == "Team offsite"
    assert message["body"] == {
        "text": "Offsite",
        "source": "text/plain",
        "truncated": True,
        "next_body_offset": 7,
        "total_chars": 21,
    }


async def test_long_body_is_read_in_parts(client: Client[Any]) -> None:
    parts: list[str] = []
    offset = 0
    while True:
        message = await call(
            client, "get_message", {"message_id": "m3", "max_body_chars": 8, "body_offset": offset}
        )
        parts.append(message["body"]["text"])
        if not message["body"]["truncated"]:
            break
        offset = message["body"]["next_body_offset"]
    assert parts == ["Offsite ", "is on Fr", "iday."]
    assert message["body"]["offset"] == 16
    assert "next_body_offset" not in message["body"]
    draft = await call(client, "get_draft", {"draft_id": "d1", "body_offset": 6})
    assert draft["message"]["body"]["text"] == "body"


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_guard.gmail_api.RETRY_BASE_S", 0)


@pytest.mark.usefixtures("no_retry_wait")
@pytest.mark.parametrize(
    "failures",
    [
        [(429, None, None), (503, None, None)],
        [(403, None, "userRateLimitExceeded")],
        [(429, "1", "rateLimitExceeded")],
    ],
)
async def test_rate_limits_and_hiccups_are_retried(
    client: Client[Any], google: Google, failures: list[tuple[int, str | None, str | None]]
) -> None:
    google.fake.gmail_failures = list(failures)
    labels = await call(client, "list_labels")
    assert labels["labels"]
    assert len(google.fake.gmail_requests) == len(failures) + 1


@pytest.mark.usefixtures("no_retry_wait")
@pytest.mark.parametrize(
    ("failures", "requests", "error"),
    [
        ([(429, None, None)] * 3, 3, "rate limit"),  # gives up after three tries
        ([(429, "60", None)], 1, "rate limit"),  # a long Retry-After isn't waited out
        # A quota for the day won't clear by retrying.
        ([(403, None, "dailyLimitExceeded")], 1, "refused the request"),
    ],
)
async def test_retries_are_bounded(
    client: Client[Any],
    google: Google,
    failures: list[tuple[int, str | None, str | None]],
    requests: int,
    error: str,
) -> None:
    google.fake.gmail_failures = list(failures)
    with pytest.raises(ToolError, match=error):
        await client.call_tool("list_labels", {})
    assert len(google.fake.gmail_requests) == requests


async def test_labels_and_drafts(client: Client[Any]) -> None:
    labels = await call(client, "list_labels")
    assert {lb["name"] for lb in labels["labels"]} == {
        "INBOX",
        "SENT",
        "UNREAD",
        "DRAFT",
        "Invoices",
    }
    drafts = await call(client, "list_drafts")
    assert drafts["drafts"][0] == {
        "id": "d1",
        "message_id": "m9",
        "thread_id": "t9",
        "subject": "Draft reply",
        "to": "ana@vendor.test",
        "date": "Wed, 4 Nov 2026 12:00:00 +0000",
        "snippet": "Draft body",
    }
    draft = await call(client, "get_draft", {"draft_id": "d1"})
    assert draft["id"] == "d1"
    assert draft["message"]["subject"] == "Draft reply"
    assert draft["message"]["body"]["text"] == "Draft body"


async def test_only_get_requests_reach_gmail(client: Client[Any], google: Google) -> None:
    for name, args in [
        ("search_threads", {}),
        ("get_thread", {"thread_id": "t1"}),
        ("get_message", {"message_id": "m1"}),
        ("list_labels", {}),
        ("list_drafts", {}),
        ("get_draft", {"draft_id": "d1"}),
    ]:
        await call(client, name, args)
    assert google.fake.write_attempts == []
    assert {method for method, _, _ in google.fake.gmail_requests} == {"GET"}


@pytest.mark.parametrize(
    "args",
    [
        {"thread_id": "../messages/send"},
        {"thread_id": "t1?x=1"},
        {"thread_id": ""},
        {"thread_id": "t1", "unexpected": True},
        {},
    ],
)
async def test_invalid_arguments_never_reach_gmail(
    client: Client[Any], google: Google, args: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        await client.call_tool("get_thread", args)
    assert google.fake.gmail_requests == []


async def test_errors_are_readable(google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = create_guard("gmail", upstream("gmail", url=google.url))
    _as(monkeypatch, google.fake.issue([GMAIL_READONLY]))
    async with Client(guard) as client:
        with pytest.raises(ToolError, match="couldn't find"):
            await client.call_tool("get_thread", {"thread_id": "nope"})
    _as(monkeypatch, google.fake.issue(["openid"]))  # Gmail refuses a token without gmail.readonly
    async with Client(guard) as client:
        with pytest.raises(ToolError, match="403"):
            await client.call_tool("list_labels", {})
    _as(monkeypatch, None)
    async with Client(guard) as client:
        with pytest.raises(ToolError, match="Not signed in"):
            await client.call_tool("list_labels", {})


async def test_unreachable_gmail_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "ya29.x")
    async with Client(create_guard("gmail", upstream("gmail", url="http://127.0.0.1:9"))) as client:
        with pytest.raises(ToolError, match="Couldn't reach the Gmail API"):
            await client.call_tool("list_labels", {})


def test_html_to_text() -> None:
    html = (
        "<html><head><title>t</title><style>p{}</style></head><body><p>Hi&nbsp;<b>there</b></p>"
        "<script>alert(1)</script><div>Line   two</div></body></html>"
    )
    assert html_to_text(html) == "Hi\xa0there\n\nLine two"
