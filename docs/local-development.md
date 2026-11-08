# Local development

Everything here runs without a Google OAuth client. For trying the guard against your real Gmail
with a pasted token, see the README's [local test mode](../README.md#local-test-mode-no-oauth-client).

## Setup and checks

```bash
uv sync
uv run pytest -q                 # everything, against fakes; no network
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

With a Redis at hand, two more tests run: storage shared between instances, and a sign-in made on
one replica used through another.

```bash
docker run -d --rm -p 6390:6379 redis:7-alpine
MCP_GUARD_TEST_REDIS_URL=redis://127.0.0.1:6390/0 uv run pytest -q
```

| Test file | Covers |
|---|---|
| `tests/test_guard.py` | The draft's guard tests: allowlist (list and call), the Gmail MCP tool fixture, domain and `hd` check, settings and `guard.yaml` validation, client registration limited to Claude, `/health`, upstream errors and unreachable upstreams; plus audit lines, pins, the size cap, the rate limit, header forwarding, DNS rebinding |
| `tests/test_google.py` | The token check: exact scopes, revocation of broader grants (deployed) and not in `--dev`, audience, `hd` |
| `tests/test_gmail_api.py` | The `gmail_api` tools against the fake Gmail API: results, GET only, id checks, errors |
| `tests/test_e2e_http.py` | Over real HTTP: a Claude-Code-style OAuth sign-in through the consent page and the fake Google, broad grants refused, outsiders told, Gmail MCP proxied with only the Google token, personal keys end to end, two replicas on Redis |
| `tests/test_storage.py` | Encryption at rest, key rotation, Redis |
| `tests/test_playground_check.py` | `scripts/playground_check.py` (the README's check with a real OAuth Playground token), run against the fake Google |

`tests/fakes/fake_google.py` fakes the Google endpoints the guard uses: OAuth (authorize, token
with PKCE and client authentication, refresh, revoke), tokeninfo, userinfo, the Gmail REST API
and Google's Gmail MCP server (every real tool name, each needing a `gmail.readonly` token).
Gmail writes are refused and recorded, so tests can assert none was attempted.

## The full sign-in on your machine

`tests/fakes/run_local.py` runs the deployed guard (OAuth sign-in and personal keys on) on
`localhost:3000`, signing people in against the fake Google on port 9000:

```bash
uv run python -m tests.fakes.run_local                        # Gmail API upstream
uv run python -m tests.fakes.run_local --upstream gmail-mcp   # fake Gmail MCP on port 9001
uv run python -m tests.fakes.run_local --hd ''                # as a personal (non-Workspace) account
```

The fake Google approves every sign-in straight away as `pat@example.com` of `example.com`. The
real `mcp-guard` command has no setting that points its sign-in anywhere but Google; only this
launcher does, so never deploy it.

### With Claude Code

```bash
claude mcp add --transport http gmail-local http://localhost:3000/mcp
claude            # then /mcp > gmail-local > Authenticate
```

Claude Code opens the guard's consent page ("Claude Code wants to access gmail-guard"); choose
Allow Access, the fake Google signs you in, and Claude Code shows "Authentication successful". Ask
it to list your labels or search for "invoice"; it sees six read-only tools and nothing that can
send, delete or label. Each call leaves an audit line in the launcher's output. Remove it after
with `claude mcp remove gmail-local`.

### With a personal key

Open `http://localhost:3000/keys`, choose **Sign in and create a key**, and copy the key. Then,
from any MCP client over Streamable HTTP:

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main(key: str) -> None:
    transport = StreamableHttpTransport("http://localhost:3000/mcp", headers={"X-API-Key": key})
    async with Client(transport) as client:
        print([t.name for t in await client.list_tools()])
        print(await client.call_tool("search_threads", {"query": "invoice"}))


asyncio.run(main("mcpg_..."))
```

**Sign in and revoke my key** on the same page makes the key stop working at once.
