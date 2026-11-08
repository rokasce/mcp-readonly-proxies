# Local development and quick start

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it).

```sh
uv sync                         # add --extra postgres for PostgreSQL support
uv run pytest -q                # unit, adapter and end-to-end tests
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

To run the store tests against PostgreSQL as well (CI does this):

```sh
docker run -d --rm --name mrp-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=mrp -p 55432:5432 postgres:16-alpine
uv sync --extra postgres
MCP_RO_TEST_POSTGRES_URL=postgresql+psycopg://postgres:pw@127.0.0.1:55432/mrp uv run pytest -q
```

## Try the whole flow without a Google OAuth client

`tests/fakes/fake_google.py` fakes the Google endpoints the proxy uses (OAuth with PKCE,
RS256 ID tokens and JWKS, tokeninfo, revocation) plus a small Gmail mailbox. Gmail
requests other than GET are refused and recorded.

```sh
# Terminal 1: fake Google on :9000 (signs everyone in as pat@example.com)
uv run python -m tests.fakes.fake_google --port 9000

# Terminal 2: the proxy on :8000, wired to the fake
export TOKEN_ENCRYPTION_KEYS="$(uv run mcp-ro-proxy generate-key --kid dev)"
export SESSION_SECRET="$(uv run mcp-ro-proxy generate-session-secret)"
uv run mcp-ro-proxy serve --config config/examples/gmail-fake-google.yaml --port 8000
```

Open <http://localhost:8000/>, choose **Sign in with Google** (the fake consents
immediately), then create a personal key on the keys page.

Use it from Claude Code:

```sh
claude mcp add --transport http gmail-ro-local http://localhost:8000/mcp \
  --header "Authorization: Bearer mrp_..."
```

or, without changing your Claude Code config, with a one-off MCP config file:

```sh
cat > /tmp/gmail-ro.json <<'EOF'
{"mcpServers": {"gmail-ro": {"type": "http", "url": "http://localhost:8000/mcp",
  "headers": {"Authorization": "Bearer mrp_..."}}}}
EOF
claude -p "List the gmail-ro tools, then search threads for 'invoice'." \
  --mcp-config /tmp/gmail-ro.json --strict-mcp-config \
  --allowedTools "mcp__gmail-ro__search_threads"
```

Or from Python with the FastMCP client:

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    transport = StreamableHttpTransport("http://localhost:8000/mcp", auth="mrp_...")
    async with Client(transport) as client:
        print([t.name for t in await client.list_tools()])
        print(await client.call_tool("search_threads", {"query": "invoice"}))


asyncio.run(main())
```

The proxy prints one JSON audit line per decision on stdout.

## With a real Google OAuth client

Create a Web OAuth client as in [deploy.md](deploy.md#2-create-the-google-oauth-client)
with the redirect URI `http://localhost:8000/auth/callback` (Google allows `http` for
localhost), then:

```sh
export PUBLIC_BASE_URL=http://localhost:8000
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_WORKSPACE_DOMAIN=example.com
export TOKEN_ENCRYPTION_KEYS="$(uv run mcp-ro-proxy generate-key --kid dev)"
export SESSION_SECRET="$(uv run mcp-ro-proxy generate-session-secret)"
uv run mcp-ro-proxy serve --config config/gmail.yaml --port 8000
```

State goes to `./data/gmail-ro.db` (SQLite) unless `DATABASE_URL` is set.

## Container

```sh
docker build -f deploy/Dockerfile -t mcp-ro-proxy .
docker run --rm -p 8080:8080 -e PUBLIC_BASE_URL=http://localhost:8080 \
  -e GOOGLE_CLIENT_ID=... -e GOOGLE_CLIENT_SECRET=... -e GOOGLE_WORKSPACE_DOMAIN=example.com \
  -e TOKEN_ENCRYPTION_KEYS=... -e SESSION_SECRET=... mcp-ro-proxy
```

## Code map

| Path | What |
|---|---|
| `src/mcp_ro_proxy/config.py` | YAML schema and `${ENV}` expansion |
| `src/mcp_ro_proxy/policy.py` | Default-deny allowlist, definition pins, annotation rewrite |
| `src/mcp_ro_proxy/provider.py` | FastMCP provider exposing allowlisted, pinned tools |
| `src/mcp_ro_proxy/middleware.py` | Call-time re-check, argument validation, size cap, audit |
| `src/mcp_ro_proxy/audit.py` | JSON audit lines |
| `src/mcp_ro_proxy/upstreams/` | `gmail_native.py` (Gmail API, GET only) and `mcp_upstream.py` (filtered MCP client) |
| `src/mcp_ro_proxy/auth/google.py` | Google sign-in, scope checks, per-user access tokens |
| `src/mcp_ro_proxy/auth/personal_keys.py` | Personal API keys as a FastMCP `AuthProvider` |
| `src/mcp_ro_proxy/auth/web.py` | Sign-in and key management pages |
| `src/mcp_ro_proxy/store.py`, `crypto.py` | SQLAlchemy store, AES-GCM token encryption |
| `src/mcp_ro_proxy/app.py`, `cli.py` | Assembly and the `mcp-ro-proxy` command |
| `tests/fakes/` | Fake Google (OAuth + Gmail) and a fake upstream MCP server with read and write tools |

## Adding another upstream

- **An MCP server** (for example Payhawk): write a config with `kind: mcp`, run
  `mcp-ro-proxy pins` against it, review each tool, and allowlist only read tools.
  The proxy sends `mcp` upstreams no credential (`auth: {type: none}`); per-user
  upstream OAuth (Payhawk) is not built yet and would plug in at `McpUpstream._client`.
  A shared upstream credential is an open stakeholder decision
  ([open questions](open-questions.md)).
- **A provider API**: add a `native` provider next to `gmail_native.py` implementing the
  `Upstream` protocol in `upstreams/base.py`, with GET-only, least-privilege calls.
