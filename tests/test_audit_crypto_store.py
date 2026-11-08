from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from mcp_ro_proxy.audit import AUDIT_LOGGER_NAME, AuditLogger, JsonLineFormatter, args_sha256
from mcp_ro_proxy.crypto import CryptoError, TokenCipher, generate_key
from mcp_ro_proxy.store import KEY_PREFIX, SqlStore, TooManyKeysError, hash_key
from mcp_ro_proxy.upstreams.base import Principal

ALICE = Principal(sub="1001", email="alice@example.com", client="personal_key", credential_id="k1")

# --- audit ---------------------------------------------------------------------------


def records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


def test_tool_call_line_has_required_fields_and_no_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    audit = AuditLogger("gmail")
    secret_query = "from:boss subject:salary"
    audit.tool_call(
        principal=ALICE,
        tool="search_threads",
        decision="allow",
        reason="allowed",
        arguments={"query": secret_query},
        started=time.monotonic(),
        upstream_status="ok",
        result_bytes=42,
    )
    (line,) = records(caplog)
    assert line["event"] == "tool_call"
    assert line["upstream"] == "gmail"
    assert line["user"] == {"sub": "1001", "email": "alice@example.com"}
    assert line["client"] == "personal_key"
    assert line["credential_id"] == "k1"
    assert line["tool"] == "search_threads"
    assert line["decision"] == "allow"
    assert line["severity"] == "INFO"
    assert line["latency_ms"] >= 0
    assert line["result_bytes"] == 42
    assert line["args_sha256"] == args_sha256({"query": secret_query})
    assert secret_query not in caplog.text


def test_denials_are_warnings_and_email_can_be_omitted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    AuditLogger("gmail", log_user_email=False).tool_call(
        principal=ALICE,
        tool="send_message",
        decision="deny",
        reason="not_allowlisted",
        arguments={},
        started=time.monotonic(),
    )
    (line,) = records(caplog)
    assert line["severity"] == "WARNING"
    assert line["user"] == {"sub": "1001"}
    assert "alice@example.com" not in caplog.text


def test_formatter_passes_audit_json_through() -> None:
    fmt = JsonLineFormatter()
    audit = logging.LogRecord(AUDIT_LOGGER_NAME, logging.INFO, "", 0, '{"a": 1}', None, None)
    other = logging.LogRecord("x", logging.ERROR, "", 0, "boom %s", ("!",), None)
    assert fmt.format(audit) == '{"a": 1}'
    assert json.loads(fmt.format(other))["message"] == "boom !"


# --- crypto --------------------------------------------------------------------------


def test_cipher_roundtrip_and_context_binding() -> None:
    cipher = TokenCipher(generate_key("a"))
    token = cipher.encrypt("1//refresh", context="refresh_token|1001")
    assert "1//refresh" not in token
    assert cipher.decrypt(token, context="refresh_token|1001") == "1//refresh"
    with pytest.raises(CryptoError):
        cipher.decrypt(token, context="refresh_token|2002")


def test_cipher_rotation() -> None:
    old = generate_key("old")
    new = generate_key("new")
    token = TokenCipher(old).encrypt("secret", context="c")
    rotated = TokenCipher(f"{new},{old}")
    assert rotated.decrypt(token, context="c") == "secret"
    assert rotated.needs_rotation(token)
    fresh = rotated.encrypt("secret", context="c")
    assert fresh.split(".")[1] == "new"
    with pytest.raises(CryptoError, match="unknown key id"):
        TokenCipher(old).decrypt(fresh, context="c")


def test_cipher_rejects_tampering_and_bad_keys() -> None:
    cipher = TokenCipher(generate_key("a"))
    token = cipher.encrypt("secret", context="c")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(CryptoError):
        cipher.decrypt(tampered, context="c")
    for bad in ("", "nokid", "a:short", f"{generate_key('a')},{generate_key('a')}"):
        with pytest.raises(CryptoError):
            TokenCipher(bad)


# --- store ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> list[float]:
    return [1_800_000_000.0]


@pytest.fixture
def store(tmp_path: Path, clock: list[float]) -> SqlStore:
    return SqlStore(
        f"sqlite:///{tmp_path / 's.db'}", TokenCipher(generate_key("k")), clock=lambda: clock[0]
    )


async def test_refresh_tokens_are_encrypted_at_rest(store: SqlStore, tmp_path: Path) -> None:
    await store.save_user(
        sub="1001",
        email="a@example.com",
        hd="example.com",
        refresh_token="1//plain-refresh",
        scopes=frozenset({"openid"}),
    )
    raw = sqlite3.connect(tmp_path / "s.db").execute("select * from users").fetchall()
    assert "1//plain-refresh" not in repr(raw)
    user = await store.get_user("1001")
    assert user is not None
    assert user.refresh_token == "1//plain-refresh"
    assert user.scopes == frozenset({"openid"})
    # A later sign-in without a refresh token keeps the stored one.
    await store.save_user(
        sub="1001",
        email="a@example.com",
        hd="example.com",
        refresh_token=None,
        scopes=frozenset({"openid"}),
    )
    assert (await store.get_user("1001")).refresh_token == "1//plain-refresh"  # type: ignore[union-attr]


async def test_key_lifecycle(store: SqlStore, tmp_path: Path, clock: list[float]) -> None:
    await store.save_user(
        sub="1001", email="a@example.com", hd="example.com", refresh_token="r", scopes=frozenset()
    )
    record, raw = await store.create_api_key(
        sub="1001", label=" nexos ", ttl_seconds=100, max_active=5
    )
    assert raw.startswith(KEY_PREFIX)
    assert record.label == "nexos"
    assert raw.startswith(record.hint)
    rows = sqlite3.connect(tmp_path / "s.db").execute("select key_hash from api_keys").fetchall()
    assert rows == [(hash_key(raw),)]
    assert raw not in repr(rows)

    owner = await store.authenticate_key(raw)
    assert owner is not None
    assert (owner.sub, owner.email, owner.key.id) == ("1001", "a@example.com", record.id)
    assert (await store.list_keys("1001"))[0].last_used_at == clock[0]

    assert await store.authenticate_key(raw + "x") is None
    assert await store.authenticate_key("not-a-key") is None

    clock[0] += 101
    assert await store.authenticate_key(raw) is None  # expired

    _, raw2 = await store.create_api_key(sub="1001", label="", ttl_seconds=None, max_active=5)
    assert await store.revoke_key("2002", (await store.list_keys("1001"))[0].id) is False
    newest = (await store.list_keys("1001"))[0]
    assert newest.label == "personal key"
    assert await store.revoke_key("1001", newest.id) is True
    assert await store.revoke_key("1001", newest.id) is False
    assert await store.authenticate_key(raw2) is None


async def test_max_active_keys_and_delete_user(store: SqlStore) -> None:
    await store.save_user(
        sub="1001", email="a@example.com", hd="example.com", refresh_token="r", scopes=frozenset()
    )
    _, raw = await store.create_api_key(sub="1001", label="a", ttl_seconds=None, max_active=2)
    await store.create_api_key(sub="1001", label="b", ttl_seconds=None, max_active=2)
    with pytest.raises(TooManyKeysError):
        await store.create_api_key(sub="1001", label="c", ttl_seconds=None, max_active=2)
    assert await store.revoke_all_keys("1001") == 2
    await store.create_api_key(sub="1001", label="c", ttl_seconds=None, max_active=2)

    deleted = await store.delete_user("1001")
    assert deleted is not None
    assert deleted.refresh_token == "r"
    assert await store.get_user("1001") is None
    assert await store.list_keys("1001") == []
    assert await store.authenticate_key(raw) is None
    await store.aclose()
