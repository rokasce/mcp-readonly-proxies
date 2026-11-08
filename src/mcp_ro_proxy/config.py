"""Per-upstream YAML configuration.

One YAML file describes one proxy deployment: which upstream it fronts, which tools are
allowlisted (with pinned definition hashes and the annotations to advertise), how MCP
clients authenticate, and where state lives.

Policy (upstream, allowlist, pins, limits) is meant to be reviewed in git. Deployment
values and secrets are injected with ``${ENV_VAR}`` / ``${ENV_VAR:-default}`` references,
which are expanded when the file is loaded. Secrets are held as ``SecretStr`` so they do
not end up in reprs or logs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    """Raised when a config file cannot be loaded or is invalid."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def is_local_url(url: str) -> bool:
    return (urlsplit(url).hostname or "") in _LOCAL_HOSTS


def require_https_unless_local(url: str, what: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and is_local_url(url)):
        return url
    raise ValueError(f"{what} must use https (plain http is only allowed for localhost)")


# --- tool policy ---------------------------------------------------------------------


class ToolAnnotationsConfig(_Model):
    """Annotations the proxy advertises for an exposed tool, replacing upstream ones.

    This is a read-only proxy, so only read-only tools may be allowlisted.
    """

    readOnlyHint: Literal[True] = True
    destructiveHint: Literal[False] = False
    idempotentHint: bool = True
    openWorldHint: bool = False


class ToolRule(_Model):
    name: str
    sha256: str = Field(
        description="Pinned sha256 of the upstream definition (name, description, input schema)."
    )
    title: str | None = None
    annotations: ToolAnnotationsConfig = ToolAnnotationsConfig()
    max_response_bytes: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _TOOL_NAME.match(value):
            raise ValueError(f"invalid tool name {value!r}")
        return value

    @field_validator("sha256")
    @classmethod
    def _valid_sha(cls, value: str) -> str:
        if not _SHA256_HEX.match(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


# --- upstreams -----------------------------------------------------------------------


class GmailSettings(_Model):
    api_base_url: str = "https://gmail.googleapis.com"
    max_body_chars: int = Field(default=20_000, gt=0, le=1_000_000)
    max_concurrency: int = Field(default=8, gt=0, le=32)
    request_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("api_base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        return require_https_unless_local(value.rstrip("/"), "gmail.api_base_url")


class NativeUpstreamConfig(_Model):
    """Tools implemented in this repo against a provider API."""

    kind: Literal["native"]
    name: str
    provider: Literal["gmail"]
    gmail: GmailSettings = GmailSettings()


class NoUpstreamAuth(_Model):
    type: Literal["none"]


class StaticBearerUpstreamAuth(_Model):
    """A fixed service credential for the upstream (never the MCP client's token)."""

    type: Literal["static_bearer"]
    token: SecretStr


UpstreamAuthConfig = Annotated[
    NoUpstreamAuth | StaticBearerUpstreamAuth, Field(discriminator="type")
]


class McpUpstreamConfig(_Model):
    """An upstream MCP server that is proxied with filtering and pinning."""

    kind: Literal["mcp"]
    name: str
    url: str
    auth: UpstreamAuthConfig = NoUpstreamAuth(type="none")
    timeout_seconds: float = Field(default=60.0, gt=0)
    definition_cache_seconds: float = Field(default=30.0, ge=0)

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        return require_https_unless_local(value, "upstream.url")


UpstreamConfig = Annotated[NativeUpstreamConfig | McpUpstreamConfig, Field(discriminator="kind")]


# --- server, identity, client auth, storage ------------------------------------------


class ServerConfig(_Model):
    public_base_url: str = Field(
        description="Public origin of this proxy, e.g. https://gmail-ro.example.com"
    )
    mcp_path: str = "/mcp"
    max_response_bytes: int = Field(default=1_048_576, gt=0)
    allowed_hosts: tuple[str, ...] = ()

    @field_validator("public_base_url")
    @classmethod
    def _base(cls, value: str) -> str:
        value = value.rstrip("/")
        require_https_unless_local(value, "server.public_base_url")
        parts = urlsplit(value)
        if parts.path or parts.query or parts.fragment:
            raise ValueError("server.public_base_url must be an origin without a path")
        return value

    @field_validator("mcp_path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/":
            raise ValueError("server.mcp_path must start with / and not be the root")
        return value.rstrip("/")

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_base_url).netloc

    @property
    def is_https(self) -> bool:
        return self.public_base_url.startswith("https://")


class GoogleEndpoints(_Model):
    """Google OAuth endpoints. Only overridden to point at local fakes in tests."""

    authorization_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
    revocation_endpoint: str = "https://oauth2.googleapis.com/revoke"
    tokeninfo_endpoint: str = "https://oauth2.googleapis.com/tokeninfo"
    jwks_uri: str = "https://www.googleapis.com/oauth2/v3/certs"
    issuers: tuple[str, ...] = ("https://accounts.google.com", "accounts.google.com")

    @model_validator(mode="after")
    def _https(self) -> GoogleEndpoints:
        for field in (
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "tokeninfo_endpoint",
            "jwks_uri",
        ):
            require_https_unless_local(getattr(self, field), f"identity.endpoints.{field}")
        return self


class GoogleIdentityConfig(_Model):
    """Users sign in to the proxy with Google, using the organization's own OAuth client."""

    provider: Literal["google"]
    client_id: str
    client_secret: SecretStr
    workspace_domain: str = Field(
        description="Only accounts whose hd claim equals this are accepted"
    )
    endpoints: GoogleEndpoints = GoogleEndpoints()

    @field_validator("workspace_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or "." not in value or "@" in value:
            raise ValueError("identity.workspace_domain must be a domain such as example.com")
        return value

    @field_validator("client_id")
    @classmethod
    def _client_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity.client_id must not be empty")
        return value.strip()


class PersonalKeyAuthConfig(_Model):
    """Users get revocable personal API keys from the proxy's web page (for nexos.ai)."""

    mode: Literal["personal_key"]
    api_key_headers: tuple[str, ...] = ("x-api-key",)
    key_ttl_days: int | None = Field(default=180, gt=0)
    max_active_keys_per_user: int = Field(default=10, gt=0)

    @field_validator("api_key_headers")
    @classmethod
    def _headers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        lowered = tuple(h.strip().lower() for h in value)
        if any(not h or h == "authorization" for h in lowered):
            raise ValueError(
                "api_key_headers lists extra headers; Authorization: Bearer is always accepted"
            )
        return lowered


ClientAuthConfig = Annotated[PersonalKeyAuthConfig, Field(discriminator="mode")]


class StorageConfig(_Model):
    database_url: str = Field(description="SQLAlchemy URL, e.g. sqlite:///./data/proxy.db")
    encryption_keys: SecretStr = Field(
        description="Comma-separated 'kid:base64key' AES-256 keys; the first one encrypts"
    )
    session_secret: SecretStr = Field(description="Secret for signing web session cookies")

    @field_validator("session_secret")
    @classmethod
    def _session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("storage.session_secret must be at least 32 characters")
        return value


class AuditConfig(_Model):
    log_user_email: bool = True


class ProxyConfig(_Model):
    upstream: UpstreamConfig
    server: ServerConfig
    identity: GoogleIdentityConfig
    client_auth: ClientAuthConfig
    storage: StorageConfig
    audit: AuditConfig = AuditConfig()
    allow: tuple[ToolRule, ...] = ()

    @model_validator(mode="after")
    def _unique_rules(self) -> ProxyConfig:
        seen: set[str] = set()
        for rule in self.allow:
            if rule.name in seen:
                raise ValueError(f"tool {rule.name!r} is allowlisted more than once")
            seen.add(rule.name)
        return self


# --- loading -------------------------------------------------------------------------


def expand_env(value: Any, env: Mapping[str, str]) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in every string of a parsed YAML tree."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env and env[name] != "":
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")

        return _ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def parse_config(text: str, env: Mapping[str, str] | None = None) -> ProxyConfig:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping")
    expanded = expand_env(raw, os.environ if env is None else env)
    try:
        return ProxyConfig.model_validate(expanded)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> ProxyConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_config(text, env)
