import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.types import ASGIApp

from mcp_guard.config import GuardConfig, Settings, Upstream
from tests.fakes.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle, endpoints_for

ROOT = Path(__file__).parent.parent
GUARD_YAML = ROOT / "guard.yaml"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerThread:
    """Serves an ASGI app with uvicorn on a background thread (real HTTP)."""

    def __init__(self, app: ASGIApp, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "ServerThread":
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


Serve = Callable[..., ServerThread]


@pytest.fixture
def serve() -> Iterator[Serve]:
    servers: list[ServerThread] = []

    def start(app: ASGIApp, port: int | None = None) -> ServerThread:
        server = ServerThread(app, port or free_port()).start()
        servers.append(server)
        return server

    yield start
    for server in reversed(servers):
        server.stop()


class Google:
    """The fake Google, served over HTTP."""

    def __init__(self, fake: FakeGoogle, url: str) -> None:
        self.fake = fake
        self.url = url
        self.endpoints = endpoints_for(url)


@pytest.fixture
def google(serve: Serve) -> Google:
    fake = FakeGoogle()
    return Google(fake, serve(fake.app()).url)


def upstream(name: str = "gmail", **changes: Any) -> Upstream:
    """An upstream from the shipped guard.yaml, with fields replaced (e.g. its url, for a fake)."""
    data = GuardConfig.load(GUARD_YAML).upstream(name).model_dump(mode="json")
    return Upstream.model_validate(data | changes)


def deployed_settings(base_url: str, **env: str) -> Settings:
    return Settings.from_env(
        {
            "MCP_GUARD_UPSTREAM": "gmail",
            "MCP_GUARD_BASE_URL": base_url,
            "MCP_GUARD_GOOGLE_CLIENT_ID": CLIENT_ID,
            "MCP_GUARD_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
            "MCP_GUARD_ALLOWED_DOMAIN": "example.com",
            "MCP_GUARD_JWT_SIGNING_KEY": "test-signing-key-0123456789abcdef",
            "MCP_GUARD_STORAGE_URL": "memory://",
            **env,
        }
    )
