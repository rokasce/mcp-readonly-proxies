"""Which upstreams the guard fronts, and which of their tools are allowed (guard.yaml), plus the
deployment settings that come from the environment."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

import yaml
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

# Scopes every sign-in asks for besides the upstream's own: who the person is.
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
IDENTITY_SCOPES = ["openid", EMAIL_SCOPE]
# Where MCP clients may ask the guard to send people back after sign-in: Claude.ai / Claude
# Desktop connectors, and Claude Code (a local port). Any other client is refused at registration,
# so another app can't register itself and collect people's Gmail access.
CLAUDE_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*/callback",
    "http://127.0.0.1:*/callback",
]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    # A typo in a setting must fail loudly, not be silently ignored.
    model_config = ConfigDict(extra="forbid")


def _https(url: HttpUrl, what: str) -> HttpUrl:
    if url.scheme != "https" and url.host not in LOOPBACK:
        raise ValueError(f"{what} must be https (or a local address), got {url}")
    return url


class RateLimit(_Strict):
    # Per person, across every MCP request (tools/list included): a sustained rate with room for
    # a burst. Gmail quota is shared by everyone signed in through the same OAuth client, so one
    # runaway agent must not be able to use it all up.
    per_second: float = Field(default=2.0, gt=0)
    burst: int = Field(default=20, ge=1)


class Upstream(_Strict):
    # gmail_api: the guard's own read-only tools on the GA Gmail REST API, at `url`.
    # mcp: an MCP server at `url`, called as the signed-in person with their Google token.
    kind: Literal["gmail_api", "mcp"]
    url: HttpUrl
    # Google scopes the person grants at sign-in; their token is what the guard calls upstream with.
    google_scopes: list[str] = Field(min_length=1)
    # Exact tool names agents may see and call. Everything else is hidden and refused.
    allow: list[str] = Field(min_length=1)
    # mcp only, optional: tool name -> sha256 of its definition (name, description, input
    # schema). A pinned tool whose definition changes upstream is dropped until it is reviewed.
    pins: dict[str, str] = Field(default_factory=dict[str, str])
    # Longer tool results are cut to this size, so one huge thread can't flood the agent.
    max_response_bytes: int = Field(default=1_000_000, gt=0)
    rate_limit: RateLimit = Field(default_factory=RateLimit)

    @field_validator("url")
    @classmethod
    def _url(cls, value: HttpUrl) -> HttpUrl:
        # The person's Google token travels in this request.
        return _https(value, "url")

    @field_validator("google_scopes")
    @classmethod
    def _read_only_scopes(cls, value: list[str]) -> list[str]:
        # The guard exists to give read-only access; Google's read-only scopes end in .readonly
        # (gmail.readonly, drive.readonly, calendar.readonly ...). Anything else could write.
        writable = [s for s in value if not s.endswith(".readonly")]
        if writable:
            raise ValueError(f"google_scopes must be read-only (.readonly) scopes, got {writable}")
        return value

    @field_validator("allow")
    @classmethod
    def _allow(cls, value: list[str]) -> list[str]:
        if any(not name.strip() or name != name.strip() for name in value):
            raise ValueError(f"allow has an empty or padded tool name: {value}")
        dupes = sorted({name for name in value if value.count(name) > 1})
        if dupes:
            raise ValueError(f"allow lists a tool twice: {', '.join(dupes)}")
        return value

    @model_validator(mode="after")
    def _kind_rules(self) -> "Upstream":
        host = self.url.host or ""
        if not (host.endswith(".googleapis.com") or host in LOOPBACK):
            # Every call carries the person's Google token. Sending it to anyone but Google would
            # hand that person's mailbox to a third party.
            raise ValueError(
                f"an upstream gets the person's Google token, so it must be a "
                f"googleapis.com server (or a local address), got {host}"
            )
        if self.pins and self.kind != "mcp":
            raise ValueError("pins are for mcp upstreams; gmail_api tools are the guard's own code")
        unknown = sorted(set(self.pins) - set(self.allow))
        if unknown:
            raise ValueError(f"pins for tools that aren't allowed: {', '.join(unknown)}")
        bad = sorted(name for name, digest in self.pins.items() if not _SHA256.match(digest))
        if bad:
            raise ValueError(f"pins must be lowercase sha256 hex digests: {', '.join(bad)}")
        return self


class GuardConfig(_Strict):
    upstreams: dict[str, Upstream] = Field(min_length=1)

    @classmethod
    def load(cls, path: str | Path) -> "GuardConfig":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def upstream(self, name: str) -> Upstream:
        if name not in self.upstreams:
            known = ", ".join(sorted(self.upstreams))
            raise ValueError(f"No upstream {name!r} in the config; known: {known}.")
        return self.upstreams[name]


# Environment variable -> Settings field. MCP_GUARD_HOST and MCP_GUARD_PORT are read by the
# command line (server.main) and listed here only so they aren't taken for typos.
_ENV = {
    "MCP_GUARD_CONFIG": "config_path",
    "MCP_GUARD_UPSTREAM": "upstream_name",
    "MCP_GUARD_BASE_URL": "base_url",
    "MCP_GUARD_GOOGLE_CLIENT_ID": "google_client_id",
    "MCP_GUARD_GOOGLE_CLIENT_SECRET": "google_client_secret",
    "MCP_GUARD_JWT_SIGNING_KEY": "jwt_signing_key",
    "MCP_GUARD_ALLOWED_DOMAIN": "allowed_domain",
    "MCP_GUARD_CLIENT_REDIRECT_URIS": "client_redirect_uris",
    "MCP_GUARD_PERSONAL_KEYS": "personal_keys",
    "MCP_GUARD_STORAGE_URL": "storage_url",
    "MCP_GUARD_STORAGE_ENCRYPTION_KEYS": "storage_encryption_keys",
}
_CLI_ENV = {"MCP_GUARD_HOST", "MCP_GUARD_PORT"}
# Needed outside local test mode: the sign-in, and whose accounts may use it.
_DEPLOYED = [
    "MCP_GUARD_BASE_URL",
    "MCP_GUARD_GOOGLE_CLIENT_ID",
    "MCP_GUARD_GOOGLE_CLIENT_SECRET",
    "MCP_GUARD_ALLOWED_DOMAIN",
]
_STORAGE_SCHEMES = ("redis://", "rediss://", "file://", "memory://")


def _csv(value: object) -> object:
    """A comma-separated environment value as a list."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(_Strict):
    """Deployment settings, from the environment."""

    config_path: str = "guard.yaml"
    # Which upstream in the config this process fronts, e.g. "gmail".
    upstream_name: str
    # The guard's public URL (what people add to Claude); Google redirects back here after sign-in.
    # The sign-in settings below are needed outside local test mode (--dev).
    base_url: HttpUrl | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # Signs the guard's own tokens. Left out, it's derived from the Google client secret, so
    # rotating that secret signs everyone out; set it to keep sign-ins across a rotation.
    jwt_signing_key: str | None = None
    # Only people with a verified Google Workspace account in this domain get through.
    allowed_domain: str | None = None
    # Client redirect URI patterns allowed at registration (comma-separated in the environment).
    client_redirect_uris: list[str] = Field(
        default_factory=lambda: list(CLAUDE_REDIRECT_URIS), min_length=1
    )
    # Serve the personal-key page (/keys) and accept those keys on /mcp, for MCP clients such as
    # nexos.ai that take a per-person API key instead of signing in with OAuth.
    personal_keys: bool = False
    # Where sign-ins and personal keys are kept: redis://... (needed for more than one replica),
    # file:///path, or memory:// (lost on restart). Left out: files under FASTMCP_HOME.
    storage_url: str | None = None
    # Fernet keys that encrypt everything stored (comma-separated); the first encrypts, all
    # decrypt, so a new key goes first and an old one is dropped once nothing uses it. Left out,
    # one is derived from the JWT signing key.
    storage_encryption_keys: list[str] = Field(default_factory=list[str])

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        return value and _https(value, "MCP_GUARD_BASE_URL")

    @field_validator("allowed_domain")
    @classmethod
    def _domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        domain = value.strip().lower().removeprefix("@")
        if not domain or "." not in domain or "@" in domain or "/" in domain:
            raise ValueError(
                f"MCP_GUARD_ALLOWED_DOMAIN must be a domain like example.com, got {value!r}"
            )
        return domain

    @field_validator("client_redirect_uris", mode="before")
    @classmethod
    def _uris(cls, value: object) -> object:
        return _csv(value)

    @field_validator("storage_url")
    @classmethod
    def _storage_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(_STORAGE_SCHEMES):
            schemes = ", ".join(_STORAGE_SCHEMES)
            raise ValueError(f"MCP_GUARD_STORAGE_URL must start with {schemes}; got {value!r}")
        return value

    @field_validator("storage_encryption_keys", mode="before")
    @classmethod
    def _fernet_keys(cls, value: object) -> object:
        keys = _csv(value)
        for key in cast(list[object], keys) if isinstance(keys, list) else []:
            try:
                Fernet(str(key))
            except ValueError:
                raise ValueError(
                    "MCP_GUARD_STORAGE_ENCRYPTION_KEYS must be Fernet keys (32 url-safe base64 "
                    "bytes each); make one with: python -c 'from cryptography.fernet import "
                    "Fernet; print(Fernet.generate_key().decode())'"
                ) from None
        return cast(object, keys)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, *, dev: bool = False) -> "Settings":
        # A misspelt optional setting (say MCP_GUARD_ALLOWED_DOMIAN) would otherwise quietly let
        # every Google account in.
        known = {*_ENV, *_CLI_ENV}
        unknown = sorted(n for n in env if n.startswith("MCP_GUARD_") and n not in known)
        if unknown:
            raise ValueError(
                f"Unknown setting {', '.join(unknown)}; known: {', '.join(sorted(known))}."
            )
        required = ["MCP_GUARD_UPSTREAM"] + ([] if dev else _DEPLOYED)
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise ValueError(f"Set {', '.join(missing)}.")
        return cls.model_validate(
            {field: env[name] for name, field in _ENV.items() if env.get(name)}
        )
