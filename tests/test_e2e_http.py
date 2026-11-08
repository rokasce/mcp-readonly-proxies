"""End to end over Streamable HTTP: the guard and a fake Google (OAuth, Gmail API, Gmail MCP) on
real ports, an MCP client signing in the way Claude Code does (dynamic registration, a localhost
callback, the guard's consent page, Google), and nexos-style personal keys."""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.auth import OAuth
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.auth import AuthorizationCodeResult

from mcp_guard.server import build_app
from tests.conftest import Google, Serve, deployed_settings, free_port, upstream
from tests.fakes.fake_google import GMAIL_READONLY, FakeUser


class HeadlessOAuth(OAuth):
    """FastMCP's OAuth client with a scripted browser instead of a real one: follows the
    redirects, approves the guard's consent page, and stops at the client's own callback."""

    def __init__(self, url: str) -> None:
        super().__init__(url, client_name="Claude Code (test)")
        self.browser = httpx2.AsyncClient(follow_redirects=False, timeout=30)
        self._callback: str | None = None
        self.pages: list[str] = []

    async def redirect_handler(self, authorization_url: str) -> None:
        url = authorization_url
        for _ in range(10):
            response = await self.browser.get(url)
            if response.status_code == 200 and "consentForm" in response.text:
                self.pages.append("consent")
                fields = dict(
                    re.findall(r'name="(txn_id|csrf_token)" value="([^"]+)"', response.text)
                )
                response = await self.browser.post(
                    url, data={**fields, "submit": "true", "action": "approve"}
                )
            if response.status_code not in (302, 303, 307):
                raise AssertionError(
                    f"sign-in stopped at {url}: {response.status_code} {response.text[:300]}"
                )
            url = response.headers["location"]
            if url.startswith(f"http://localhost:{self.redirect_port}/callback"):
                self._callback = url
                return
        raise AssertionError("too many redirects")

    async def callback_handler(self) -> AuthorizationCodeResult:
        assert self._callback is not None
        query = {k: v[0] for k, v in parse_qs(urlparse(self._callback).query).items()}
        if "error" in query:
            raise RuntimeError(f"sign-in failed: {query}")
        return AuthorizationCodeResult(
            code=query["code"], state=query.get("state"), iss=query.get("iss")
        )


@dataclass
class Deployment:
    url: str
    google: Google

    @property
    def mcp(self) -> str:
        return f"{self.url}/mcp"


def deploy(
    serve: Serve,
    google: Google,
    name: str = "gmail",
    upstream_url: str | None = None,
    public_url: str | None = None,
    **env: str,
) -> Deployment:
    port = free_port()
    url = f"http://localhost:{port}"
    up = upstream(name, url=upstream_url or google.url)
    settings = deployed_settings(public_url or url, MCP_GUARD_UPSTREAM=name, **env)
    serve(build_app(settings, up, endpoints=google.endpoints), port)
    return Deployment(url, google)


@pytest.fixture
def gmail(serve: Serve, google: Google) -> Deployment:
    return deploy(serve, google, MCP_GUARD_PERSONAL_KEYS="true")


async def test_claude_code_style_sign_in_and_calls(gmail: Deployment) -> None:
    auth = HeadlessOAuth(gmail.mcp)
    async with Client(StreamableHttpTransport(gmail.mcp, auth=auth)) as client:
        tools = {t.name for t in await client.list_tools()}
        labels = json.loads((await client.call_tool("list_labels", {})).content[0].text)  # type: ignore[union-attr]
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("send_email", {"to": "x@example.com"})
    assert tools == {
        "search_threads",
        "get_thread",
        "get_message",
        "list_drafts",
        "get_draft",
        "list_labels",
    }
    assert {lb["name"] for lb in labels["labels"]} == {
        "INBOX",
        "SENT",
        "UNREAD",
        "DRAFT",
        "Invoices",
    }
    assert auth.pages == ["consent"]
    [google_request] = gmail.google.fake.authorize_requests
    assert set(google_request["scope"].split()) == {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        GMAIL_READONLY,
    }
    assert google_request["hd"] == "example.com"
    assert google_request["access_type"] == "offline"
    assert google_request["include_granted_scopes"] == "false"
    assert gmail.google.fake.write_attempts == []


async def test_sign_in_outlives_googles_access_token(gmail: Deployment) -> None:
    """Google's access tokens last an hour; the client then refreshes through the guard, which
    refreshes at Google, and the new token goes through the same checks."""
    gmail.google.fake.access_lifetime_s = 2
    async with Client(StreamableHttpTransport(gmail.mcp, auth=HeadlessOAuth(gmail.mcp))) as client:
        await client.call_tool("list_labels", {})
        await asyncio.sleep(3)
        await client.call_tool("list_labels", {})
    assert gmail.google.fake.token_grants == ["authorization_code", "refresh_token"]


async def test_sign_in_with_too_much_access_fails_and_is_revoked(gmail: Deployment) -> None:
    gmail.google.fake.grant_scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        GMAIL_READONLY,
        "https://mail.google.com/",
    ]
    with pytest.raises(Exception):  # noqa: B017, PT011 - the client's own OAuth error
        async with Client(
            StreamableHttpTransport(gmail.mcp, auth=HeadlessOAuth(gmail.mcp))
        ) as client:
            await client.list_tools()
    assert gmail.google.fake.revocations, "the broad Google grant should have been revoked"
    assert gmail.google.fake.gmail_requests == []


async def test_person_outside_the_domain_gets_told(gmail: Deployment) -> None:
    gmail.google.fake.user = FakeUser(
        sub="2002", email="pat@example.com", hd=None
    )  # consumer account
    async with Client(StreamableHttpTransport(gmail.mcp, auth=HeadlessOAuth(gmail.mcp))) as client:
        with pytest.raises(Exception, match=r"Only example\.com accounts"):
            await client.list_tools()
        with pytest.raises(ToolError, match=r"Only example\.com accounts"):
            await client.call_tool("list_labels", {})
    assert gmail.google.fake.gmail_requests == []


async def test_proxied_gmail_mcp_gets_the_google_token_only(serve: Serve, google: Google) -> None:
    # The fake Gmail MCP checks tokens against the same fake Google that signs people in.
    gmail_mcp = serve(google.fake.gmail_mcp().http_app(path="/mcp/v1", stateless_http=True))
    deployment = deploy(
        serve,
        google,
        "gmail-mcp",
        upstream_url=f"{gmail_mcp.url}/mcp/v1",
        MCP_GUARD_PERSONAL_KEYS="true",
    )
    key = await create_key(deployment)
    headers = {"X-API-Key": key, "Cookie": "session=agent-cookie"}
    async with Client(StreamableHttpTransport(deployment.mcp, headers=headers)) as client:
        assert len(await client.list_tools()) == 6
        result = await client.call_tool("search_threads", {"id": "x"})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("trash_thread", {"id": "x"})
    assert result.data == "search_threads ok"
    assert google.fake.mcp_calls == ["search_threads"]
    sent = google.fake.mcp_headers[-1]
    assert sent["authorization"].startswith("Bearer ya29.fake-")
    assert "x-api-key" not in sent
    assert "cookie" not in sent


# --- personal keys (nexos.ai) ----------------------------------------------------------------


async def keys_page(
    deployment: Deployment, action: str, browser: httpx2.AsyncClient | None = None
) -> httpx2.Response:
    """Signs in on /keys with the fake Google and returns the final page."""
    browser = browser or httpx2.AsyncClient(follow_redirects=True, timeout=30)
    return await browser.get(f"{deployment.url}/keys/login?action={action}")


async def create_key(deployment: Deployment) -> str:
    page = await keys_page(deployment, "create")
    assert page.status_code == 200, page.text
    match = re.search(r'<code class="secret">(mcpg_[^<]+)</code>', page.text)
    assert match, page.text
    return match.group(1)


async def mcp_status(deployment: Deployment, key: str) -> int:
    """The HTTP status of an MCP request made with `key`."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    headers = {"X-API-Key": key, "Accept": "application/json, text/event-stream"}
    async with httpx2.AsyncClient() as http:
        return (await http.post(deployment.mcp, json=initialize, headers=headers)).status_code


async def test_personal_key_reads_gmail(
    gmail: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_guard.audit")
    key = await create_key(gmail)
    for headers in ({"Authorization": f"Bearer {key}"}, {"X-API-Key": key}):
        async with Client(StreamableHttpTransport(gmail.mcp, headers=headers)) as client:
            result = await client.call_tool("search_threads", {"query": "invoice"})
            with pytest.raises(ToolError, match="Unknown tool"):
                await client.call_tool("trash_thread", {"thread_id": "t1"})
        assert json.loads(result.content[0].text)["threads"][0]["id"] == "t1"  # type: ignore[union-attr]
    [request] = [
        r
        for r in gmail.google.fake.authorize_requests
        if r["redirect_uri"].endswith("/keys/callback")
    ]
    assert set(request["scope"].split()) == {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        GMAIL_READONLY,
    }
    audit = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp_guard.audit"]
    assert audit[0]["client"] == "personal_key"
    assert audit[0]["person"] == "pat@example.com"
    assert gmail.google.fake.write_attempts == []


async def test_personal_key_outlives_googles_access_token(gmail: Deployment) -> None:
    key = await create_key(gmail)
    # Refreshed this long before it expires, so the guard's copy is good for 2 seconds.
    gmail.google.fake.access_lifetime_s = 122
    for _ in range(2):
        assert await mcp_status(gmail, key) == 200
    await asyncio.sleep(3)
    assert await mcp_status(gmail, key) == 200
    assert gmail.google.fake.token_grants == [
        "authorization_code",
        "refresh_token",
        "refresh_token",
    ]


async def test_new_key_replaces_old_and_revoke_works(gmail: Deployment) -> None:
    first = await create_key(gmail)
    second = await create_key(gmail)
    assert await mcp_status(gmail, second) == 200
    assert await mcp_status(gmail, first) == 401
    page = await keys_page(gmail, "revoke")
    assert "is revoked" in page.text
    # Revoking asks Google only who the person is, not for Gmail access.
    assert (
        gmail.google.fake.authorize_requests[-1]["scope"]
        == "openid https://www.googleapis.com/auth/userinfo.email"
    )
    assert await mcp_status(gmail, second) == 401
    assert await mcp_status(gmail, "mcpg_made-up") == 401


async def test_key_page_refuses_outsiders_and_broad_grants(gmail: Deployment) -> None:
    gmail.google.fake.user = FakeUser(sub="3003", email="mallory@other.example", hd="other.example")
    page = await keys_page(gmail, "create")
    assert page.status_code == 403
    assert "Only example.com accounts" in page.text
    assert "mcpg_" not in page.text
    gmail.google.fake.user = FakeUser()
    gmail.google.fake.grant_scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        GMAIL_READONLY,
        "https://www.googleapis.com/auth/gmail.send",
    ]
    page = await keys_page(gmail, "create")
    assert page.status_code == 403
    assert "mcpg_" not in page.text
    assert len(gmail.google.fake.revocations) == 2  # both refused grants dropped at Google


async def test_key_page_sign_in_is_tied_to_the_browser(gmail: Deployment) -> None:
    async with httpx2.AsyncClient(follow_redirects=False) as attacker:
        start = await attacker.get(f"{gmail.url}/keys/login?action=create")
        google = await attacker.get(start.headers["location"])
    # The victim's browser follows the attacker's callback link, without the attacker's cookie.
    async with httpx2.AsyncClient(follow_redirects=False) as victim:
        page = await victim.get(google.headers["location"])
    assert page.status_code == 400
    assert "mcpg_" not in page.text


async def test_keys_are_stored_hashed_and_tokens_encrypted(
    serve: Serve, google: Google, tmp_path: Any
) -> None:
    deployment = deploy(
        serve, google, MCP_GUARD_PERSONAL_KEYS="true", MCP_GUARD_STORAGE_URL=f"file://{tmp_path}"
    )
    key = await create_key(deployment)
    stored = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert stored
    assert key not in stored
    refresh_tokens = [t for t, g in google.fake._refresh.items() if not g.revoked]  # pyright: ignore[reportPrivateUsage]
    assert refresh_tokens
    assert not any(token in stored for token in refresh_tokens)
    assert "pat@example.com" not in stored


async def test_personal_keys_off_by_default(serve: Serve, google: Google) -> None:
    deployment = deploy(serve, google)
    async with httpx2.AsyncClient() as http:
        assert (await http.get(f"{deployment.url}/keys")).status_code == 404
        assert (await http.get(f"{deployment.url}/health")).json() == {
            "status": "ok",
            "upstream": "gmail",
        }


@pytest.mark.skipif(
    not os.environ.get("GUARD_TEST_REDIS_URL"), reason="needs GUARD_TEST_REDIS_URL"
)
async def test_sign_ins_work_on_every_replica(serve: Serve, google: Google) -> None:
    """Two replicas behind one public URL, sharing Redis: a person signs in on one and is served by
    the other, with an OAuth token and with a personal key."""
    env = {
        "MCP_GUARD_STORAGE_URL": os.environ["GUARD_TEST_REDIS_URL"],
        "MCP_GUARD_STORAGE_ENCRYPTION_KEYS": "4j0xKqTt0bHq7oG1QeYyF6O8jx1oI5yX9Zl1p2nQf2w=",
        "MCP_GUARD_PERSONAL_KEYS": "true",
    }
    first = deploy(serve, google, **env)
    second = deploy(serve, google, public_url=first.url, **env)
    auth = HeadlessOAuth(first.mcp)
    async with Client(StreamableHttpTransport(first.mcp, auth=auth)) as client:
        await client.list_tools()
    tokens = await auth.token_storage_adapter.get_tokens()  # pyright: ignore[reportOptionalMemberAccess]
    assert tokens is not None
    headers = {"Authorization": f"Bearer {tokens.access_token}"}
    async with Client(StreamableHttpTransport(second.mcp, headers=headers)) as client:
        assert len(await client.list_tools()) == 6
    key = await create_key(first)
    assert await mcp_status(second, key) == 200
