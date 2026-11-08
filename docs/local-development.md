# Local development

Nothing here needs a Google OAuth client. For your real Gmail with a pasted token, see the README's
[local test mode](../README.md#local-test-mode-no-oauth-client).

## Setup and checks

```bash
uv sync
uv run pytest -q                 # all tests, against fakes, no network
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

Two more tests (shared storage, a sign-in used across replicas) run when a Redis is available:

```bash
docker run -d --rm -p 6390:6379 redis:7-alpine
GUARD_TEST_REDIS_URL=redis://127.0.0.1:6390/0 uv run pytest -q
```

| Test file | Covers |
|---|---|
| `tests/test_guard.py` | Allowlist, domain check, config, client registration, audit, pins, limits, headers |
| `tests/test_google.py` | The token check: exact scopes, revoking broader grants, audience, `hd` |
| `tests/test_gmail_api.py` | The `gmail_api` tools: results, GET only, id checks, errors |
| `tests/test_e2e_http.py` | Over real HTTP: OAuth sign-in, Gmail MCP proxying, personal keys, replicas |
| `tests/test_storage.py` | Encryption at rest, key rotation, Redis |
| `tests/test_playground_check.py` | `scripts/playground_check.py`, against the fake Google |

`tests/fakes/fake_google.py` fakes Google's sign-in, Gmail API and Gmail MCP, and records any
attempted write.

## The full sign-in on your machine

`tests/fakes/run_local.py` runs the deployed guard (OAuth and personal keys on) on
`localhost:3000`, signing in against the fake Google on port 9000:

```bash
uv run python -m tests.fakes.run_local                        # Gmail API upstream
uv run python -m tests.fakes.run_local --upstream gmail-mcp   # fake Gmail MCP on port 9001
uv run python -m tests.fakes.run_local --hd ''                # as a non-Workspace account
```

The fake Google approves every sign-in as `pat@example.com`. Never deploy this launcher: the real
`mcp-guard` command can't point its sign-in anywhere but Google.

### With Claude Code

```bash
claude mcp add --transport http gmail-local http://localhost:3000/mcp
claude            # then /mcp > gmail-local > Authenticate
```

See [Claude Code's MCP sign-in](https://code.claude.com/docs/en/mcp#authenticate-with-remote-mcp-servers). Choose Allow Access on the
consent page. Claude Code sees six read-only tools; ask it to search
for "invoice". Remove it with `claude mcp remove gmail-local`.

### With a personal key

Open `http://localhost:3000/keys`, choose **Sign in and create a key**, and use the key from any
MCP client:

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

**Sign in and revoke my key** on the same page stops the key at once.
