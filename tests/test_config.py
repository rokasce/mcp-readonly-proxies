from __future__ import annotations

from pathlib import Path

import pytest

from mcp_ro_proxy.config import (
    ConfigError,
    McpUpstreamConfig,
    NativeUpstreamConfig,
    ProxyConfig,
    load_config,
    parse_config,
)
from mcp_ro_proxy.crypto import generate_key
from tests.conftest import REPO, config_dict

ENV = {
    "PUBLIC_BASE_URL": "https://gmail-ro.example.com",
    "GOOGLE_CLIENT_ID": "abc.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "shh",
    "GOOGLE_WORKSPACE_DOMAIN": "Example.com",
    "TOKEN_ENCRYPTION_KEYS": generate_key("k1"),
    "SESSION_SECRET": "x" * 48,
}


def test_shipped_gmail_config_loads_with_env() -> None:
    config = load_config(REPO / "config" / "gmail.yaml", env=ENV)
    assert isinstance(config.upstream, NativeUpstreamConfig)
    assert config.server.public_base_url == "https://gmail-ro.example.com"
    assert config.identity.workspace_domain == "example.com"
    assert config.storage.database_url == "sqlite:///./data/gmail-ro.db"
    assert [r.name for r in config.allow] == [
        "search_threads",
        "get_thread",
        "get_message",
        "list_labels",
        "list_drafts",
    ]
    assert all(r.annotations.readOnlyHint for r in config.allow)
    # Secrets never show up in reprs.
    assert "shh" not in repr(config)


def test_payhawk_example_parses_and_fails_closed() -> None:
    config = load_config(REPO / "config" / "examples" / "payhawk.yaml", env=ENV)
    assert isinstance(config.upstream, McpUpstreamConfig)
    assert config.upstream.url == "https://mcp.payhawk.com/mcp"
    assert {r.sha256 for r in config.allow} == {"0" * 64}


def test_missing_env_var_is_an_error() -> None:
    env = {k: v for k, v in ENV.items() if k != "GOOGLE_CLIENT_SECRET"}
    with pytest.raises(ConfigError, match="GOOGLE_CLIENT_SECRET is not set"):
        load_config(REPO / "config" / "gmail.yaml", env=env)


def test_env_default_is_used() -> None:
    config = load_config(REPO / "config" / "gmail.yaml", env={**ENV, "DATABASE_URL": "sqlite:///x"})
    assert config.storage.database_url == "sqlite:///x"


def test_write_annotations_cannot_be_allowlisted(tmp_path: Path) -> None:
    data = config_dict(
        tmp_path,
        allow=[{"name": "send", "sha256": "a" * 64, "annotations": {"readOnlyHint": False}}],
    )
    with pytest.raises(ValueError, match="readOnlyHint"):
        ProxyConfig.model_validate(data)
    data["allow"] = [
        {"name": "trash", "sha256": "a" * 64, "annotations": {"destructiveHint": True}}
    ]
    with pytest.raises(ValueError, match="destructiveHint"):
        ProxyConfig.model_validate(data)


def test_duplicate_and_malformed_rules_rejected(tmp_path: Path) -> None:
    rule = {"name": "list_labels", "sha256": "b" * 64}
    with pytest.raises(ValueError, match="more than once"):
        ProxyConfig.model_validate(config_dict(tmp_path, allow=[rule, rule]))
    with pytest.raises(ValueError, match="64 lowercase hex"):
        ProxyConfig.model_validate(config_dict(tmp_path, allow=[{"name": "x", "sha256": "B" * 64}]))
    with pytest.raises(ValueError, match="invalid tool name"):
        ProxyConfig.model_validate(
            config_dict(tmp_path, allow=[{"name": "a b", "sha256": "b" * 64}])
        )


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    data = config_dict(tmp_path)
    data["server"]["forward_auth"] = True
    with pytest.raises(ValueError, match="forward_auth"):
        ProxyConfig.model_validate(data)


@pytest.mark.parametrize(
    "url",
    ["http://gmail-ro.example.com", "https://gmail-ro.example.com/prefix", "ftp://x.example"],
)
def test_public_base_url_must_be_https_origin(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="public_base_url"):
        ProxyConfig.model_validate(config_dict(tmp_path, base_url=url))


def test_plain_http_allowed_for_localhost(tmp_path: Path) -> None:
    config = ProxyConfig.model_validate(config_dict(tmp_path, base_url="http://localhost:8000/"))
    assert config.server.public_base_url == "http://localhost:8000"
    assert config.server.public_host == "localhost:8000"
    assert not config.server.is_https


def test_upstream_mcp_url_must_be_https(tmp_path: Path) -> None:
    upstream = {"kind": "mcp", "name": "x", "url": "http://mcp.example.com/mcp"}
    with pytest.raises(ValueError, match=r"upstream\.url"):
        ProxyConfig.model_validate(config_dict(tmp_path, upstream=upstream))


def test_authorization_is_not_an_extra_api_key_header(tmp_path: Path) -> None:
    data = config_dict(tmp_path)
    data["client_auth"] = {"mode": "personal_key", "api_key_headers": ["Authorization"]}
    with pytest.raises(ValueError, match="always accepted"):
        ProxyConfig.model_validate(data)


def test_short_session_secret_rejected(tmp_path: Path) -> None:
    data = config_dict(tmp_path)
    data["storage"]["session_secret"] = "short"
    with pytest.raises(ValueError, match="at least 32"):
        ProxyConfig.model_validate(data)


def test_parse_errors_are_config_errors() -> None:
    with pytest.raises(ConfigError, match="mapping"):
        parse_config("- just a list", env={})
    with pytest.raises(ConfigError, match="invalid YAML"):
        parse_config("a: [", env={})
