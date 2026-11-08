"""The deployed guard on this machine, signing people in against a fake Google, so the whole
sign-in (Claude Code's OAuth flow, the personal-key page) can be tried without a Google OAuth
client:

    uv run python -m tests.fakes.run_local                      # Gmail API upstream
    uv run python -m tests.fakes.run_local --upstream gmail-mcp # Gmail MCP upstream (fake)

The fake Google approves every sign-in as pat@example.com straight away. Never deploy this: the
real guard (`mcp-guard`) has no way to point its sign-in anywhere but Google.
"""

import argparse
import threading
import time

import uvicorn

from mcp_guard.server import build_app, configure_logging
from tests.conftest import deployed_settings, upstream
from tests.fakes.fake_google import FakeGoogle, FakeUser, endpoints_for


def _serve(app: object, port: int) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")  # type: ignore[arg-type]
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--upstream", choices=["gmail", "gmail-mcp"], default="gmail")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--google-port", type=int, default=9000)
    parser.add_argument("--email", default="pat@example.com")
    parser.add_argument(
        "--hd", default="example.com", help="Workspace domain; '' for a consumer account"
    )
    args = parser.parse_args()

    fake = FakeGoogle(user=FakeUser(email=args.email, hd=args.hd or None))
    google = f"http://127.0.0.1:{args.google_port}"
    _serve(fake.app(), args.google_port)
    upstream_url = google
    if args.upstream == "gmail-mcp":
        _serve(fake.gmail_mcp().http_app(path="/mcp/v1", stateless_http=True), args.google_port + 1)
        upstream_url = f"http://127.0.0.1:{args.google_port + 1}/mcp/v1"

    configure_logging()
    base = f"http://localhost:{args.port}"
    settings = deployed_settings(
        base, MCP_GUARD_UPSTREAM=args.upstream, MCP_GUARD_PERSONAL_KEYS="true"
    )
    app = build_app(
        settings, upstream(args.upstream, url=upstream_url), endpoints=endpoints_for(google)
    )
    print(
        f"\nGuard ({args.upstream}) on {base}/mcp, signing in with the fake Google on {google}.\n"
        f"  Claude Code:  claude mcp add --transport http gmail-local {base}/mcp\n"
        f"                (then /mcp in Claude Code to sign in)\n"
        f"  Personal key: open {base}/keys\n",
        flush=True,
    )
    time.sleep(0.5)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None)


if __name__ == "__main__":
    main()
