"""End to end over real HTTP: browser sign-in, personal key, MCP client over Streamable HTTP.

Two uvicorn servers run on localhost: the fake Google (OAuth + Gmail API) and the proxy
built from config exactly as ``mcp-ro-proxy serve`` builds it.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_ro_proxy.app import Proxy, build_proxy
from mcp_ro_proxy.auth.google import GMAIL_READONLY, USERINFO_EMAIL
from tests.conftest import REPO, free_port, make_config
from tests.fakes.fake_google import FakeGoogle
from tests.fakes.fake_upstream import UpstreamCalls, build_fake_upstream


@dataclass
class Env:
    fake: FakeGoogle
    proxy: Proxy
    base: str
    browser: httpx2.AsyncClient


def gmail_allow() -> list[dict[str, Any]]:
    import yaml

    raw = yaml.safe_load((REPO / "config" / "gmail.yaml").read_text())
    return raw["allow"]


@pytest.fixture
async def env(tmp_path: Path, serve: Any) -> AsyncIterator[Env]:
    fake = FakeGoogle()
    google = serve(fake.app())
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    config = make_config(
        tmp_path,
        base_url=base,
        google_url=google.url,
        upstream={
            "kind": "native",
            "name": "gmail",
            "provider": "gmail",
            "gmail": {"api_base_url": google.url},
        },
        allow=gmail_allow(),
    )
    proxy = build_proxy(config)
    serve(proxy.app, port)
    async with httpx2.AsyncClient(follow_redirects=False, timeout=10) as browser:
        yield Env(fake=fake, proxy=proxy, base=base, browser=browser)


async def sign_in(env: Env) -> httpx2.Response:
    """Drive the browser flow: proxy -> Google -> proxy callback. Returns the final response."""
    resp = await env.browser.get(f"{env.base}/auth/login")
    assert resp.status_code == 302
    resp = await env.browser.get(resp.headers["location"])  # fake Google consents
    assert resp.status_code == 302
    return await env.browser.get(resp.headers["location"])  # proxy callback


def csrf_of(page: str) -> str:
    match = re.search(r"name='csrf' value='([^']+)'", page)
    assert match, page
    return match.group(1)


async def create_key(env: Env, label: str = "nexos") -> str:
    page = (await env.browser.get(f"{env.base}/keys")).text
    resp = await env.browser.post(f"{env.base}/keys", data={"csrf": csrf_of(page), "label": label})
    assert resp.status_code == 200
    match = re.search(r"id='new-key'>(mrp_[A-Za-z0-9_-]+)<", resp.text)
    assert match, resp.text
    return match.group(1)


def mcp_client(env: Env, key: str | None = None, *, header: str | None = None) -> Client[Any]:
    headers = {header: key} if header and key else None
    auth = key if key and not header else None
    return Client(StreamableHttpTransport(f"{env.base}/mcp", headers=headers, auth=auth))


async def mcp_status(env: Env, key: str) -> int:
    resp = await env.browser.post(
        f"{env.base}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
    )
    return resp.status_code


def text(result: Any) -> str:
    return result.content[0].text


async def test_gmail_end_to_end(env: Env) -> None:
    # Landing page and sign-in.
    landing = await env.browser.get(f"{env.base}/")
    assert "Sign in with Google" in landing.text
    final = await sign_in(env)
    assert final.status_code == 303
    assert final.headers["location"] == "/keys"

    # Google was asked for exactly openid, email and gmail.readonly, offline.
    (authz,) = env.fake.authorize_requests
    assert authz["scope"] == f"openid email {GMAIL_READONLY}"
    assert authz["access_type"] == "offline"
    assert authz["include_granted_scopes"] == "false"

    key = await create_key(env)

    async with mcp_client(env, key) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {
            "search_threads",
            "get_thread",
            "get_message",
            "list_labels",
            "list_drafts",
        }
        for tool in tools.values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False

        found = json.loads(text(await client.call_tool("search_threads", {"query": "invoice"})))
        assert found["threads"][0]["subject"] == "Q3 invoice"
        thread = json.loads(text(await client.call_tool("get_thread", {"thread_id": "t1"})))
        assert thread["messages"][1]["body"]["text"] == "Thanks, received."

        for write_tool in ("send_message", "create_draft", "trash_thread", "label_message"):
            result = await client.call_tool_mcp(write_tool, {})
            assert result.is_error
            assert "Unknown tool" in text(result)

    # Gmail only ever saw GETs carrying the user's Google token, never the personal key.
    assert env.fake.write_attempts == []
    assert env.fake.gmail_authorizations
    assert all(a.startswith("Bearer ya29.fake-") for a in env.fake.gmail_authorizations)
    assert not any(key in a for a in env.fake.gmail_authorizations)

    # The X-API-Key header works too.
    async with mcp_client(env, key, header="X-API-Key") as client:
        assert len(await client.list_tools()) == 5


async def test_mcp_requires_a_valid_key(env: Env) -> None:
    resp = await env.browser.post(
        f"{env.base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert resp.status_code == 401
    resp = await env.browser.post(
        f"{env.base}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer mrp_not-a-real-key"},
    )
    assert resp.status_code == 401
    with pytest.raises(Exception, match="error response"):
        async with mcp_client(env, "mrp_forged") as client:
            await client.list_tools()


async def test_revoked_key_stops_working(env: Env) -> None:
    await sign_in(env)
    key = await create_key(env)
    async with mcp_client(env, key) as client:
        assert await client.list_tools()
    assert await mcp_status(env, key) != 401
    page = (await env.browser.get(f"{env.base}/keys")).text
    key_id = re.search(r"action='/keys/([0-9a-f]+)/revoke'", page)
    assert key_id
    resp = await env.browser.post(
        f"{env.base}/keys/{key_id.group(1)}/revoke", data={"csrf": csrf_of(page)}
    )
    assert resp.status_code == 303
    assert "revoked" in (await env.browser.get(f"{env.base}/keys")).text
    assert await mcp_status(env, key) == 401


async def test_disconnect_revokes_google_grant_and_keys(env: Env) -> None:
    await sign_in(env)
    key = await create_key(env)
    user = await env.proxy.store.get_user("1001")
    assert user is not None
    assert user.refresh_token
    page = (await env.browser.get(f"{env.base}/keys")).text
    resp = await env.browser.post(f"{env.base}/disconnect", data={"csrf": csrf_of(page)})
    assert resp.status_code == 303
    assert user.refresh_token in env.fake.revoked
    assert await env.proxy.store.get_user("1001") is None
    assert await mcp_status(env, key) == 401
    landing = await env.browser.get(f"{env.base}/?disconnected=1")
    assert "Disconnected" in landing.text


async def test_sign_in_with_write_scope_is_refused(env: Env) -> None:
    env.fake.grant_scopes = [
        "openid",
        USERINFO_EMAIL,
        GMAIL_READONLY,
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    resp = await sign_in(env)
    assert resp.status_code == 403
    assert "write scopes" in resp.text
    assert await env.proxy.store.get_user("1001") is None
    assert env.fake.revoked


async def test_sign_in_from_another_domain_is_refused(env: Env) -> None:
    env.fake.user.hd = "evil.test"
    env.fake.user.email = "mallory@evil.test"
    resp = await sign_in(env)
    assert resp.status_code == 403
    assert "Only example.com" in resp.text
    assert await env.proxy.store.get_user("1001") is None


async def test_callback_rejects_forged_state_and_csrf(env: Env) -> None:
    resp = await env.browser.get(f"{env.base}/auth/login")
    google = await env.browser.get(resp.headers["location"])
    callback = urlsplit(google.headers["location"])
    code = parse_qs(callback.query)["code"][0]
    forged = await env.browser.get(f"{env.base}/auth/callback?code={code}&state=forged")
    assert forged.status_code == 400
    assert "state did not match" in forged.text

    await sign_in(env)
    resp = await env.browser.post(f"{env.base}/keys", data={"csrf": "wrong", "label": "x"})
    assert resp.status_code == 403
    assert await env.proxy.store.list_keys("1001") == []


async def test_web_pages_send_security_headers(env: Env) -> None:
    resp = await env.browser.get(f"{env.base}/")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert (await env.browser.get(f"{env.base}/keys")).status_code == 303
    assert (await env.browser.get(f"{env.base}/healthz")).text == "ok"
    # Browser form posts carry the page's Origin; foreign or opaque origins are refused.
    await sign_in(env)
    page = (await env.browser.get(f"{env.base}/keys")).text
    for origin, status in (("https://evil.test", 403), ("null", 403), (env.base, 200)):
        resp = await env.browser.post(
            f"{env.base}/keys",
            data={"csrf": csrf_of(page), "label": "o"},
            headers={"Origin": origin},
        )
        assert resp.status_code == status, origin
    # Requests for another Host are refused (DNS-rebinding protection).
    wrong_host = await env.browser.get(f"{env.base}/healthz", headers={"Host": "evil.test"})
    assert wrong_host.status_code == 421


class RecordHeaders:
    def __init__(self, app: ASGIApp, seen: list[dict[str, str]]) -> None:
        self.app = app
        self.seen = seen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await self.app(scope, receive, send)


async def test_mcp_upstream_over_http_never_receives_client_credentials(
    tmp_path: Path, serve: Any
) -> None:
    calls = UpstreamCalls()
    upstream = build_fake_upstream(calls)
    seen: list[dict[str, str]] = []
    upstream_server = serve(RecordHeaders(upstream.http_app(path="/mcp"), seen))
    upstream_cfg = {
        "kind": "mcp",
        "name": "payhawk",
        "url": f"{upstream_server.url}/mcp",
        "auth": {"type": "static_bearer", "token": "upstream-service-token"},
    }
    from mcp_ro_proxy.config import McpUpstreamConfig
    from mcp_ro_proxy.policy import tool_sha256
    from mcp_ro_proxy.upstreams.mcp_upstream import McpUpstream

    pins = {
        t.name: tool_sha256(t)
        for t in await McpUpstream(McpUpstreamConfig.model_validate(upstream_cfg)).list_tools(None)
    }
    seen.clear()
    port = free_port()
    config = make_config(
        tmp_path,
        base_url=f"http://127.0.0.1:{port}",
        upstream=upstream_cfg,
        allow=[{"name": "list_expenses", "sha256": pins["list_expenses"]}],
    )
    proxy = build_proxy(config)
    serve(proxy.app, port)
    await proxy.store.save_user(
        sub="1001",
        email="pat@example.com",
        hd="example.com",
        refresh_token=None,
        scopes=frozenset({"openid"}),
    )
    _, key = await proxy.store.create_api_key(sub="1001", label="t", ttl_seconds=None, max_active=5)

    transport = StreamableHttpTransport(
        f"http://127.0.0.1:{port}/mcp", auth=key, headers={"X-API-Key": key, "Cookie": "c=1"}
    )
    async with Client(transport) as client:
        assert [t.name for t in await client.list_tools()] == ["list_expenses"]
        await client.call_tool("list_expenses", {"limit": 1})
        denied = await client.call_tool_mcp("create_expense", {"amount": 5})
        assert denied.is_error

    assert calls.calls == [("list_expenses", {"limit": 1})]
    assert seen, "the upstream was never contacted"
    for headers in seen:
        assert headers.get("authorization") == "Bearer upstream-service-token"
        assert key not in json.dumps(headers)
        assert "x-api-key" not in headers
        assert "cookie" not in headers
