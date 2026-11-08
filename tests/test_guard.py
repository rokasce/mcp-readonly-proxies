import json
import logging
from typing import Any

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.providers.proxy import ProxyClient

from mcp_guard.config import GuardConfig, Settings, Upstream
from mcp_guard.gmail_api import add_gmail_tools
from mcp_guard.server import create_guard, definition_sha256, google_auth, user_token_client
from tests.conftest import GUARD_YAML, deployed_settings, upstream
from tests.fakes.fake_google import GMAIL_MCP_TOOLS, FakeGoogle

READ_ONLY_IN_GMAIL = {t["name"] for t in GMAIL_MCP_TOOLS if t["annotations"].get("readOnlyHint")}


@pytest.fixture
def gmail_mcp() -> Upstream:
    return upstream("gmail-mcp")


@pytest.fixture
def fake() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def fake_gmail() -> FastMCP[Any]:
    """An upstream with every real Gmail MCP tool name; records which ones were called."""
    server: FastMCP[Any] = FastMCP("fake-gmail")
    server.called = []  # type: ignore[attr-defined]
    for spec in GMAIL_MCP_TOOLS:

        def tool(id: str = "", _name: str = spec["name"]) -> str:
            server.called.append(_name)  # type: ignore[attr-defined]
            return f"{_name} ok"

        server.tool(tool, name=spec["name"])
    return server


def proxied(gmail_mcp: Upstream, fake_gmail: FastMCP[Any], **kwargs: Any) -> FastMCP[Any]:
    return create_guard(
        "gmail", gmail_mcp, client_factory=lambda: ProxyClient(fake_gmail), **kwargs
    )


@pytest.fixture
def guard(gmail_mcp: Upstream, fake_gmail: FastMCP[Any]) -> FastMCP[Any]:
    return proxied(gmail_mcp, fake_gmail)


def test_allowlist_is_read_only_in_gmail() -> None:
    config = GuardConfig.load(GUARD_YAML)
    assert set(config.upstream("gmail-mcp").allow) == READ_ONLY_IN_GMAIL
    # Both Gmail upstreams offer the same tools, so switching changes nothing for agents.
    assert set(config.upstream("gmail").allow) == READ_ONLY_IN_GMAIL


async def test_gmail_api_tools_are_the_read_only_gmail_tools() -> None:
    server: FastMCP[Any] = FastMCP("x")
    add_gmail_tools(server, "https://gmail.googleapis.com")
    tools = await server.list_tools()
    assert {t.name for t in tools} == READ_ONLY_IN_GMAIL
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_lists_only_allowed_tools(guard: FastMCP[Any], gmail_mcp: Upstream) -> None:
    async with Client(guard) as client:
        assert {t.name for t in await client.list_tools()} == set(gmail_mcp.allow)


async def test_forwards_allowed_tool(guard: FastMCP[Any], fake_gmail: Any) -> None:
    async with Client(guard) as client:
        result = await client.call_tool("search_threads", {"id": "x"})
    assert result.data == "search_threads ok"
    assert fake_gmail.called == ["search_threads"]


@pytest.mark.parametrize(
    "tool", ["create_draft", "trash_thread", "label_message", "create_label", "send_email"]
)
async def test_refuses_other_tools_without_reaching_upstream(
    guard: FastMCP[Any], fake_gmail: Any, tool: str
) -> None:
    async with Client(guard) as client:
        with pytest.raises(ToolError):
            await client.call_tool(tool, {"id": "x"})
    assert fake_gmail.called == []


async def test_refuses_other_tools_on_gmail_api() -> None:
    guard = create_guard("gmail", upstream("gmail", allow=["list_labels"]))
    async with Client(guard) as client:
        assert [t.name for t in await client.list_tools()] == ["list_labels"]
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("search_threads", {})


async def test_resources_and_prompts_are_not_proxied(
    gmail_mcp: Upstream, fake_gmail: FastMCP[Any]
) -> None:
    @fake_gmail.resource("gmail://secrets")
    def secrets_resource() -> str:
        return "should never be proxied"

    @fake_gmail.prompt()
    def send_everything() -> str:
        return "send all drafts"

    async with Client(proxied(gmail_mcp, fake_gmail)) as client:
        assert await client.list_resources() == []
        assert await client.list_prompts() == []
        with pytest.raises(Exception, match="not found"):
            await client.read_resource("gmail://secrets")


async def test_domain_check_refuses_unknown_person(gmail_mcp: Upstream, fake_gmail: Any) -> None:
    async with Client(proxied(gmail_mcp, fake_gmail, allowed_domain="example.com")) as client:
        with pytest.raises(ToolError, match=r"example\.com"):
            await client.call_tool("search_threads", {"id": "x"})
    assert fake_gmail.called == []


def test_upstream_client_needs_sign_in(gmail_mcp: Upstream) -> None:
    with pytest.raises(ToolError, match="Not signed in"):
        user_token_client(gmail_mcp)()


def test_upstream_client_sends_only_the_google_token(
    monkeypatch: pytest.MonkeyPatch, gmail_mcp: Upstream
) -> None:
    _signed_in(monkeypatch, google_token="ya29.person")
    client = user_token_client(gmail_mcp)()
    transport = client.transport
    assert isinstance(transport, StreamableHttpTransport)
    assert transport.headers == {"Authorization": "Bearer ya29.person"}
    # The agent's own headers (a personal key in X-API-Key, say) are not passed on.
    assert client._transport_options is not None  # pyright: ignore[reportPrivateUsage]
    assert client._transport_options.forward_incoming_headers is False  # pyright: ignore[reportPrivateUsage]


def test_unknown_upstream() -> None:
    with pytest.raises(ValueError, match="other-vendor"):
        GuardConfig.load(GUARD_YAML).upstream("other-vendor")


def test_settings_require_google_client_and_domain() -> None:
    with pytest.raises(ValueError, match=r"MCP_GUARD_GOOGLE_CLIENT_ID.*MCP_GUARD_ALLOWED_DOMAIN"):
        Settings.from_env(
            {"MCP_GUARD_UPSTREAM": "gmail", "MCP_GUARD_BASE_URL": "https://guard.example"}
        )


@pytest.fixture
async def signin_app(gmail_mcp: Upstream, fake_gmail: FastMCP[Any]) -> Any:
    auth, routes = google_auth(deployed_settings("https://guard.example"), gmail_mcp)
    guard = proxied(gmail_mcp, fake_gmail, auth=auth, routes=routes)
    return guard.http_app()


@pytest.mark.parametrize(
    ("redirect_uri", "accepted"),
    [
        ("https://claude.ai/api/mcp/auth_callback", True),
        ("http://localhost:54321/callback", True),
        ("https://evil.example/callback", False),
        ("http://localhost.evil.example:1/callback", False),
    ],
)
async def test_only_claude_clients_can_register(
    signin_app: Any, redirect_uri: str, accepted: bool
) -> None:
    async with signin_app.router.lifespan_context(signin_app):
        transport = httpx2.ASGITransport(app=signin_app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="https://guard.example"
        ) as http:
            response = await http.post(
                "/register",
                json={
                    "redirect_uris": [redirect_uri],
                    "client_name": "test",
                    "token_endpoint_auth_method": "none",
                },
            )
    assert (response.status_code == 201) is accepted, response.text


async def test_sign_in_asks_google_for_read_only_access_only(signin_app: Any) -> None:
    async with signin_app.router.lifespan_context(signin_app):
        transport = httpx2.ASGITransport(app=signin_app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="https://guard.example"
        ) as http:
            metadata = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert set(metadata["scopes_supported"]) == {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.readonly",
    }


def test_redirect_uris_from_env() -> None:
    settings = deployed_settings(
        "https://guard.example",
        MCP_GUARD_CLIENT_REDIRECT_URIS="https://claude.ai/api/mcp/auth_callback, http://localhost:*/callback",
    )
    assert settings.client_redirect_uris == [
        "https://claude.ai/api/mcp/auth_callback",
        "http://localhost:*/callback",
    ]


def test_dev_settings_need_only_upstream() -> None:
    assert Settings.from_env({"MCP_GUARD_UPSTREAM": "gmail"}, dev=True).upstream_name == "gmail"


def _signed_in(
    monkeypatch: pytest.MonkeyPatch, google_token: str = "ya29.t", **claims: Any
) -> None:
    access = AccessToken(token=google_token, client_id="c", scopes=[], claims=claims)
    monkeypatch.setattr("mcp_guard.server.get_access_token", lambda: access)


PAT = {
    "email": "pat@example.com",
    "email_verified": True,
    "google_user_data": {"hd": "example.com"},
}


@pytest.mark.parametrize(
    ("claims", "accepted"),
    [
        (PAT, True),
        ({**PAT, "email": "Pat@example.com"}, True),
        # As Google's tokeninfo sends it: strings, not booleans.
        ({**PAT, "email_verified": "true"}, True),
        ({**PAT, "email_verified": "false"}, False),
        ({**PAT, "email_verified": False}, False),
        ({"email": "pat@example.com", "google_user_data": {"hd": "example.com"}}, False),
        # A consumer Google account for a company address has no hd: refused.
        ({**PAT, "google_user_data": None}, False),
        ({**PAT, "google_user_data": {"hd": "other.com"}}, False),
        ({**PAT, "email": "pat@notexample.com"}, False),
        ({**PAT, "email": "pat@example.com.evil.example"}, False),
    ],
)
async def test_domain_check(
    monkeypatch: pytest.MonkeyPatch,
    gmail_mcp: Upstream,
    fake_gmail: Any,
    claims: dict[str, Any],
    accepted: bool,
) -> None:
    _signed_in(monkeypatch, **claims)
    async with Client(proxied(gmail_mcp, fake_gmail, allowed_domain="example.com")) as client:
        if accepted:
            await client.call_tool("search_threads", {"id": "x"})
        else:
            with pytest.raises(ToolError, match=r"example\.com"):
                await client.call_tool("search_threads", {"id": "x"})
            with pytest.raises(Exception, match=r"example\.com"):
                await client.list_tools()
    assert fake_gmail.called == (["search_threads"] if accepted else [])


def test_misspelt_setting_refused() -> None:
    with pytest.raises(ValueError, match="MCP_GUARD_ALLOWED_DOMIAN"):
        Settings.from_env(
            {"MCP_GUARD_UPSTREAM": "gmail", "MCP_GUARD_ALLOWED_DOMIAN": "example.com"}, dev=True
        )


def test_host_and_port_are_known_settings() -> None:
    Settings.from_env(
        {"MCP_GUARD_UPSTREAM": "gmail", "MCP_GUARD_HOST": "127.0.0.1", "MCP_GUARD_PORT": "3000"},
        dev=True,
    )


@pytest.mark.parametrize(
    ("value", "expected"), [("example.com", "example.com"), (" @Example.com ", "example.com")]
)
def test_allowed_domain_normalised(value: str, expected: str) -> None:
    env = {"MCP_GUARD_UPSTREAM": "gmail", "MCP_GUARD_ALLOWED_DOMAIN": value}
    assert Settings.from_env(env, dev=True).allowed_domain == expected


@pytest.mark.parametrize("value", ["me@example.com", "example", "https://example.com"])
def test_allowed_domain_must_be_a_domain(value: str) -> None:
    with pytest.raises(ValueError, match="MCP_GUARD_ALLOWED_DOMAIN"):
        Settings.from_env(
            {"MCP_GUARD_UPSTREAM": "gmail", "MCP_GUARD_ALLOWED_DOMAIN": value}, dev=True
        )


def test_base_url_must_be_https() -> None:
    with pytest.raises(ValueError, match="https"):
        deployed_settings("http://guard.example")
    deployed_settings("http://localhost:3000")


@pytest.mark.parametrize(
    ("env", "error"),
    [
        ({"MCP_GUARD_STORAGE_URL": "postgres://db"}, "MCP_GUARD_STORAGE_URL"),
        ({"MCP_GUARD_STORAGE_ENCRYPTION_KEYS": "not-a-key"}, "Fernet"),
        ({"MCP_GUARD_PERSONAL_KEYS": "maybe"}, "personal_keys"),
    ],
)
def test_bad_settings_refused(env: dict[str, str], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        deployed_settings("https://guard.example", **env)


GOOD = {
    "kind": "mcp",
    "url": "https://gmailmcp.googleapis.com/mcp/v1",
    "google_scopes": ["x.readonly"],
    "allow": ["a"],
}


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"url": "http://gmail.example/mcp"}, "https"),
        ({"allow": ["a", "a"]}, "twice"),
        ({"allow": ["a "]}, "padded"),
        ({"alow": ["b"]}, "alow"),
        ({"kind": "rest"}, "kind"),
        # Read-only scopes only: a writable scope is refused before anyone signs in.
        ({"google_scopes": ["https://www.googleapis.com/auth/gmail.modify"]}, "read-only"),
        ({"google_scopes": ["https://mail.google.com/"]}, "read-only"),
        # The person's Google token must not be sent to anyone but Google.
        ({"url": "https://mcp.payhawk.com/mcp"}, "googleapis.com"),
        ({"pins": {"b": "0" * 64}}, "aren't allowed"),
        ({"pins": {"a": "abc"}}, "sha256"),
        (
            {"kind": "gmail_api", "url": "https://gmail.googleapis.com", "pins": {"a": "0" * 64}},
            "gmail_api",
        ),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"rate_limit": {"per_second": 1, "burts": 5}}, "burts"),
    ],
)
def test_bad_upstream_config_refused(changes: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        GuardConfig.model_validate({"upstreams": {"x": GOOD | changes}})


async def test_health_needs_no_sign_in(signin_app: Any) -> None:
    async with signin_app.router.lifespan_context(signin_app):
        transport = httpx2.ASGITransport(app=signin_app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="https://guard.example"
        ) as http:
            health = await http.get("/health")
            mcp = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "upstream": "gmail"}
    assert mcp.status_code == 401


async def test_upstream_tool_error_reaches_the_agent(gmail_mcp: Upstream) -> None:
    """The upstream's own error results are meant for clients (e.g. "thread not found"), so they
    pass through; mask_error_details only hides the guard's own exceptions."""
    broken: FastMCP[Any] = FastMCP("broken-gmail")

    @broken.tool(name="search_threads")
    def search_threads() -> str:
        raise ValueError("Thread abc not found")

    async with Client(proxied(gmail_mcp, broken)) as client:
        with pytest.raises(ToolError, match="Thread abc not found"):
            await client.call_tool("search_threads", {})


async def test_unreachable_upstream_is_an_error_not_a_hang(gmail_mcp: Upstream) -> None:
    def unreachable() -> ProxyClient[Any]:
        return ProxyClient(StreamableHttpTransport("http://127.0.0.1:9/mcp"), timeout=5)

    async with Client(create_guard("gmail", gmail_mcp, client_factory=unreachable)) as client:
        with pytest.raises(Exception, match="Couldn't reach gmail"):
            await client.list_tools()
        with pytest.raises(ToolError, match="Couldn't reach gmail"):
            await client.call_tool("search_threads", {})
        # A tool that isn't allowed is still just refused.
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("trash_thread", {})


def _audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp_guard.audit"]


async def test_audit_line_after_each_call(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gmail_mcp: Upstream,
    fake_gmail: Any,
) -> None:
    _signed_in(monkeypatch, **PAT)
    caplog.set_level(logging.INFO, logger="mcp_guard.audit")
    async with Client(proxied(gmail_mcp, fake_gmail, allowed_domain="example.com")) as client:
        await client.call_tool("search_threads", {"id": "secret-query"})
        with pytest.raises(ToolError):
            await client.call_tool("trash_thread", {"id": "t1"})
    allowed, refused = _audit_lines(caplog)
    assert allowed["person"] == "pat@example.com"
    assert allowed["client"] == "oauth"
    assert (allowed["tool"], allowed["decision"], allowed["outcome"]) == (
        "search_threads",
        "allow",
        "ok",
    )
    assert isinstance(allowed["duration_ms"], int)
    assert (refused["tool"], refused["decision"], refused["outcome"]) == (
        "trash_thread",
        "deny",
        "refused",
    )
    # Arguments (search queries, ids) are never logged.
    assert "secret-query" not in caplog.text
    assert "t1" not in json.dumps(refused)


async def test_tool_error_is_audited_as_error(
    caplog: pytest.LogCaptureFixture, gmail_mcp: Upstream
) -> None:
    broken: FastMCP[Any] = FastMCP("broken-gmail")

    @broken.tool(name="get_thread")
    def get_thread() -> str:
        raise ValueError("Thread abc not found")

    caplog.set_level(logging.INFO, logger="mcp_guard.audit")
    async with Client(proxied(gmail_mcp, broken)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_thread", {})
    [line] = _audit_lines(caplog)
    assert (line["decision"], line["outcome"]) == ("allow", "error")


async def test_pinned_tool_dropped_when_its_definition_changes(
    caplog: pytest.LogCaptureFixture, gmail_mcp: Upstream, fake_gmail: FastMCP[Any]
) -> None:
    tools = {t.name: t for t in await proxied(gmail_mcp, fake_gmail).list_tools()}
    pins = {"search_threads": definition_sha256(tools["search_threads"]), "get_thread": "0" * 64}
    guard = proxied(upstream("gmail-mcp", pins=pins), fake_gmail)
    async with Client(guard) as client:
        listed = {t.name for t in await client.list_tools()}
        assert "search_threads" in listed
        assert "get_thread" not in listed
        await client.call_tool("search_threads", {})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("get_thread", {})
    assert fake_gmail.called == ["search_threads"]  # type: ignore[attr-defined]
    assert f"now {definition_sha256(tools['get_thread'])}" in caplog.text


async def test_long_results_are_cut(gmail_mcp: Upstream) -> None:
    chatty: FastMCP[Any] = FastMCP("chatty-gmail")

    @chatty.tool(name="get_thread")
    def get_thread() -> str:
        return "x" * 5000

    guard = proxied(upstream("gmail-mcp", max_response_bytes=1000), chatty)
    async with Client(guard) as client:
        result = await client.call_tool("get_thread", {})
    text = result.content[0].text  # type: ignore[union-attr]
    assert len(text.encode()) <= 1000
    assert text.endswith("[Response truncated due to size limit]")


async def test_rate_limit_is_per_person(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fake_gmail: FastMCP[Any]
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_guard.audit")
    guard = proxied(upstream("gmail-mcp", rate_limit={"per_second": 0.001, "burst": 3}), fake_gmail)
    _signed_in(monkeypatch, **PAT)
    async with Client(guard) as client:
        await client.call_tool("search_threads", {})  # initialize + list + call use up the burst
        with pytest.raises(Exception, match="Rate limit exceeded"):
            await client.call_tool("search_threads", {})
    _signed_in(monkeypatch, **{**PAT, "email": "lee@example.com"})
    async with Client(guard) as client:
        await client.call_tool("search_threads", {})
    assert _audit_lines(caplog)[1]["outcome"] == "rate_limited"
