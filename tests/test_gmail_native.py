from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from mcp_ro_proxy.auth.google import GMAIL_READONLY
from mcp_ro_proxy.config import GmailSettings
from mcp_ro_proxy.upstreams.base import Principal, UpstreamError
from mcp_ro_proxy.upstreams.gmail_native import GmailNativeUpstream, html_to_text
from tests.fakes.fake_google import CLIENT_ID, FakeGoogle

PAT = Principal(sub="1001", email="pat@example.com", client="personal_key")


class Tokens:
    """Access-token source backed by the fake: issues readonly tokens, counts refreshes."""

    def __init__(self, fake: FakeGoogle, scopes: list[str] | None = None) -> None:
        self.fake = fake
        self.scopes = scopes or [GMAIL_READONLY]
        self.current: str | None = None
        self.issued = 0

    async def get(self, principal: Principal) -> str:
        if self.current is None:
            self.current = self.fake._issue_access(self.scopes, CLIENT_ID)
            self.issued += 1
        return self.current

    def invalidate(self, sub: str) -> None:
        self.current = None


@pytest.fixture
def fake() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
async def gmail(fake: FakeGoogle) -> AsyncIterator[tuple[GmailNativeUpstream, Tokens]]:
    tokens = Tokens(fake)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake.app())) as http:
        settings = GmailSettings(api_base_url="http://127.0.0.1:9000", max_body_chars=1000)
        yield GmailNativeUpstream("gmail", settings, tokens=tokens, http=http), tokens


async def call(up: GmailNativeUpstream, name: str, args: dict[str, Any]) -> Any:
    result = await up.call_tool(PAT, name, args)
    assert not result.is_error
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_tool_definitions(gmail: tuple[GmailNativeUpstream, Tokens]) -> None:
    up, _ = gmail
    tools = {t.name: t for t in await up.list_tools(None)}
    assert set(tools) == {
        "search_threads",
        "get_thread",
        "get_message",
        "list_labels",
        "list_drafts",
    }
    assert tools["search_threads"].input_schema["additionalProperties"] is False
    assert up.google_scopes == frozenset({GMAIL_READONLY})
    assert await up.get_tool(None, "send_message") is None


async def test_search_threads(gmail: tuple[GmailNativeUpstream, Tokens], fake: FakeGoogle) -> None:
    up, _ = gmail
    data = await call(up, "search_threads", {"query": "invoice", "max_results": 5})
    assert [t["id"] for t in data["threads"]] == ["t1"]
    thread = data["threads"][0]
    assert thread["subject"] == "Q3 invoice"
    assert thread["from"] == "Ana <ana@vendor.test>"
    assert thread["message_count"] == 2
    assert thread["last_message_date"] == "Mon, 2 Nov 2026 10:30:00 +0000"
    assert "INBOX" in thread["label_ids"]
    listing = fake.gmail_requests[0]
    assert listing[0] == "GET"
    assert "q=invoice" in listing[2]
    assert "maxResults=5" in listing[2]


async def test_get_thread_and_message_bodies(gmail: tuple[GmailNativeUpstream, Tokens]) -> None:
    up, _ = gmail
    thread = await call(up, "get_thread", {"thread_id": "t1"})
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

    headers_only = await call(up, "get_thread", {"thread_id": "t1", "include_bodies": False})
    assert "body" not in headers_only["messages"][0]

    message = await call(up, "get_message", {"message_id": "m3", "max_body_chars": 7})
    assert message["subject"] == "Team offsite"
    assert message["body"] == {"text": "Offsite", "source": "text/plain", "truncated": True}


async def test_labels_and_drafts(gmail: tuple[GmailNativeUpstream, Tokens]) -> None:
    up, _ = gmail
    labels = await call(up, "list_labels", {})
    assert {lbl["name"] for lbl in labels["labels"]} == {"INBOX", "Invoices"}
    drafts = await call(up, "list_drafts", {})
    assert drafts["drafts"][0]["subject"] == "Draft reply"
    assert drafts["drafts"][0]["to"] == "ana@vendor.test"
    assert drafts["drafts"][0]["date"] == "Wed, 4 Nov 2026 12:00:00 +0000"


async def test_only_get_requests_reach_gmail(
    gmail: tuple[GmailNativeUpstream, Tokens], fake: FakeGoogle
) -> None:
    up, _ = gmail
    for name, args in [
        ("search_threads", {}),
        ("get_thread", {"thread_id": "t1"}),
        ("get_message", {"message_id": "m1"}),
        ("list_labels", {}),
        ("list_drafts", {}),
    ]:
        await call(up, name, args)
    assert fake.write_attempts == []
    assert {method for method, _, _ in fake.gmail_requests} == {"GET"}


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
    gmail: tuple[GmailNativeUpstream, Tokens], fake: FakeGoogle, args: dict[str, Any]
) -> None:
    up, _ = gmail
    with pytest.raises(UpstreamError, match="Invalid arguments"):
        await up.call_tool(PAT, "get_thread", args)
    assert fake.gmail_requests == []


async def test_expired_access_token_is_refreshed_once(
    gmail: tuple[GmailNativeUpstream, Tokens], fake: FakeGoogle
) -> None:
    up, tokens = gmail
    await call(up, "list_labels", {})
    fake.expire_access_tokens()
    await call(up, "list_labels", {})
    assert tokens.issued == 2


async def test_errors_are_client_safe(gmail: tuple[GmailNativeUpstream, Tokens]) -> None:
    up, tokens = gmail
    with pytest.raises(UpstreamError, match="could not find"):
        await up.call_tool(PAT, "get_thread", {"thread_id": "nope"})
    with pytest.raises(UpstreamError, match="authenticated user"):
        await up.call_tool(None, "list_labels", {})
    tokens.scopes = ["openid"]  # a token without gmail.readonly is refused by Gmail
    tokens.invalidate("1001")
    with pytest.raises(UpstreamError, match="403"):
        await up.call_tool(PAT, "list_labels", {})


def test_html_to_text() -> None:
    html = (
        "<html><head><title>t</title><style>p{}</style></head><body><p>Hi&nbsp;<b>there</b></p>"
        "<script>alert(1)</script><div>Line   two</div></body></html>"
    )
    assert html_to_text(html) == "Hi\xa0there\n\nLine two"
