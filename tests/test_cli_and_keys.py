from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastmcp.server.auth import AccessToken
from starlette.types import Message, Receive, Scope, Send

from mcp_ro_proxy.auth.personal_keys import ApiKeyHeaderMiddleware, PersonalKeyAuth
from mcp_ro_proxy.auth.principal import principal_from_access_token
from mcp_ro_proxy.cli import main
from mcp_ro_proxy.crypto import TokenCipher
from mcp_ro_proxy.policy import tool_sha256
from mcp_ro_proxy.store import SqlStore
from mcp_ro_proxy.upstreams.gmail_native import NATIVE_TOOLS
from tests.conftest import REPO
from tests.test_config import ENV


def test_shipped_gmail_pins_match_the_native_tools() -> None:
    """Changing a Gmail tool's name, description or schema must come with a pin update."""
    allow = yaml.safe_load((REPO / "config" / "gmail.yaml").read_text())["allow"]
    pins = {rule["name"]: rule["sha256"] for rule in allow}
    assert pins == {t.name: tool_sha256(t.definition()) for t in NATIVE_TOOLS}


def test_cli_pins_reports_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pins", "--config", str(REPO / "config" / "gmail.yaml")]) == 0
    out = capsys.readouterr().out
    assert out.count("allowlisted, pin ok") == 5
    assert "- name: search_threads" in out


def test_cli_generate_key_and_check_config(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["generate-key", "--kid", "prod1"]) == 0
    key = capsys.readouterr().out.strip()
    assert key.startswith("prod1:")
    TokenCipher(key)  # parses
    assert main(["generate-session-secret"]) == 0
    assert len(capsys.readouterr().out.strip()) >= 32

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    assert main(["check-config", "--config", str(REPO / "config" / "gmail.yaml")]) == 0
    assert "5 allowlisted tools" in capsys.readouterr().out
    monkeypatch.delenv("SESSION_SECRET")
    assert main(["check-config", "--config", str(REPO / "config" / "gmail.yaml")]) == 2
    assert "SESSION_SECRET" in capsys.readouterr().err


async def run_header_middleware(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    seen: list[tuple[bytes, bytes]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.extend(scope["headers"])

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        return None

    mw = ApiKeyHeaderMiddleware(app, ("x-api-key",))
    await mw({"type": "http", "headers": headers}, receive, send)
    return seen


async def test_api_key_header_becomes_bearer() -> None:
    seen = await run_header_middleware([(b"x-api-key", b" mrp_abc ")])
    assert (b"authorization", b"Bearer mrp_abc") in seen


async def test_explicit_authorization_wins() -> None:
    seen = await run_header_middleware(
        [(b"authorization", b"Bearer mrp_a"), (b"x-api-key", b"mrp_b")]
    )
    assert [v for k, v in seen if k == b"authorization"] == [b"Bearer mrp_a"]


async def test_verifier_maps_keys_to_principals(tmp_path: Path) -> None:
    from mcp_ro_proxy.crypto import generate_key

    store = SqlStore(f"sqlite:///{tmp_path / 'k.db'}", TokenCipher(generate_key("k")))
    await store.save_user(
        sub="1001", email="pat@example.com", hd="example.com", refresh_token="r", scopes=frozenset()
    )
    record, raw = await store.create_api_key(sub="1001", label="x", ttl_seconds=60, max_active=5)
    auth = PersonalKeyAuth(store)
    token = await auth.verify_token(raw)
    assert token is not None
    assert raw not in token.token  # the raw key is not carried on the request
    assert token.expires_at == record.expires_at
    principal = principal_from_access_token(token)
    assert principal is not None
    assert (principal.sub, principal.email, principal.client, principal.credential_id) == (
        "1001",
        "pat@example.com",
        "personal_key",
        record.id,
    )
    assert await auth.verify_token("mrp_wrong") is None
    assert principal_from_access_token(None) is None
    assert principal_from_access_token(AccessToken(token="t", client_id="c", scopes=[])) is None
    await store.aclose()
