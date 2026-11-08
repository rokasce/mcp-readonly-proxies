from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.types import ASGIApp

from mcp_ro_proxy.config import ProxyConfig
from mcp_ro_proxy.crypto import generate_key
from tests.fakes.fake_google import CLIENT_ID, CLIENT_SECRET, endpoints_for

REPO = Path(__file__).resolve().parent.parent


def config_dict(
    tmp_path: Path,
    *,
    base_url: str = "http://localhost:8000",
    google_url: str = "http://127.0.0.1:9000",
    upstream: dict[str, Any] | None = None,
    allow: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "upstream": upstream or {"kind": "native", "name": "gmail", "provider": "gmail"},
        "server": {"public_base_url": base_url},
        "identity": {
            "provider": "google",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "workspace_domain": "example.com",
            "endpoints": endpoints_for(google_url),
        },
        "client_auth": {"mode": "personal_key"},
        "storage": {
            "database_url": f"sqlite:///{tmp_path / 'proxy.db'}",
            "encryption_keys": generate_key("test"),
            "session_secret": "s" * 40,
        },
        "allow": allow or [],
    }
    for key, value in overrides.items():
        data[key] = value
    return data


def make_config(tmp_path: Path, **kwargs: Any) -> ProxyConfig:
    return ProxyConfig.model_validate(config_dict(tmp_path, **kwargs))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerThread:
    """Serve an ASGI app with uvicorn on a background thread (real HTTP for e2e tests)."""

    def __init__(self, app: ASGIApp, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> ServerThread:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[ServerThread] = []

    def start(app: ASGIApp, port: int | None = None) -> ServerThread:
        server = ServerThread(app, port or free_port()).__enter__()
        servers.append(server)
        return server

    yield start
    for server in reversed(servers):
        server.__exit__(None, None, None)
