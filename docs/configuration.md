# Configuration

One YAML file configures one proxy deployment: one upstream, its allowlist, how MCP
clients authenticate, and where state lives. The shipped files are:

| File | Purpose |
|---|---|
| `config/gmail.yaml` | Gmail read-only proxy (production config, values from the environment) |
| `config/examples/gmail-fake-google.yaml` | Same proxy wired to the local fake Google, for trials ([local-development.md](local-development.md)) |
| `config/examples/payhawk.yaml` | Example `mcp` upstream; not deployable yet (see below) |

Validate a file and its environment with:

```sh
uv run mcp-ro-proxy check-config --config config/gmail.yaml
```

## Environment references

Strings may contain `${VAR}` or `${VAR:-default}`. They are expanded when the file is
loaded; a missing variable without a default is an error. Keep policy (upstream,
allowlist, pins, limits) literal in git, and inject deployment values and secrets:

| Variable (in `config/gmail.yaml`) | Secret? | Meaning |
|---|---|---|
| `PUBLIC_BASE_URL` | no | Public origin of the proxy, e.g. `https://gmail-ro.example.com`. The OAuth redirect URI is `${PUBLIC_BASE_URL}/auth/callback` and the MCP endpoint `${PUBLIC_BASE_URL}/mcp` |
| `GOOGLE_CLIENT_ID` | no | The organization's own Web OAuth client ([deploy.md](deploy.md#2-create-the-google-oauth-client)) |
| `GOOGLE_CLIENT_SECRET` | yes | Its secret |
| `GOOGLE_WORKSPACE_DOMAIN` | no | Only Google accounts whose `hd` claim equals this domain can sign in |
| `DATABASE_URL` | yes (has a password) | SQLAlchemy URL. Default `sqlite:///./data/gmail-ro.db` (local only) |
| `TOKEN_ENCRYPTION_KEYS` | yes | Key ring for stored refresh tokens: `kid:base64key[,kid:base64key...]`. Generate with `mcp-ro-proxy generate-key --kid k1` |
| `SESSION_SECRET` | yes | Signs the web session cookie (at least 32 characters). Generate with `mcp-ro-proxy generate-session-secret` |
| `PORT` | no | Listen port for `serve` (Cloud Run sets it) |
| `PROXY_CONFIG` | no | Config path inside the container image (default `/app/config/gmail.yaml`) |

## Sections

### `upstream`

Two kinds share the same policy core.

**`native`**: tools implemented in this repo against a provider API. Only `provider: gmail`
exists today.

```yaml
upstream:
  kind: native
  name: gmail
  provider: gmail
  gmail:                          # all optional
    api_base_url: https://gmail.googleapis.com
    max_body_chars: 20000         # per message body; a tool argument can only lower it
    max_concurrency: 8            # parallel Gmail reads per call
    request_timeout_seconds: 30
```

The Gmail tools are `search_threads`, `get_thread`, `get_message`, `list_labels` and
`list_drafts`. Every request is a GET made with the calling user's own token, which
holds only `gmail.readonly`.

**`mcp`**: proxy an existing MCP server, exposing only allowlisted, pinned tools.

```yaml
upstream:
  kind: mcp
  name: payhawk
  url: https://mcp.payhawk.com/mcp      # https required (http only for localhost)
  auth: {type: none}                    # the only mode: no upstream credential
  timeout_seconds: 60
  definition_cache_seconds: 30          # how long call-time pin checks reuse a listing
```

The proxy is an ordinary MCP client of the upstream. It never forwards the MCP client's
credentials or other request headers, and it never relays resources, prompts, sampling,
elicitation or roots, and it sends the upstream no credential at all.
**Per-user upstream OAuth for `mcp` upstreams (what Payhawk needs) is not built yet**:
Payhawk accepts sign-ins only from allowlisted apps, so it first needs Payhawk Support to
allowlist the proxy's redirect URI. A shared upstream credential (for example one Payhawk
API key used for every signed-in user) is deliberately not supported; whether that is
acceptable is an open stakeholder decision ([open questions](open-questions.md)). The
place where per-user upstream OAuth would plug in is `McpUpstream._client` in
`src/mcp_ro_proxy/upstreams/mcp_upstream.py`.

### `server`

```yaml
server:
  public_base_url: ${PUBLIC_BASE_URL}
  mcp_path: /mcp
  max_response_bytes: 1048576     # larger tool results are replaced by an error
  allowed_hosts: []               # extra Host header values to accept besides the public one
```

Requests whose `Host` is not the public host (or `allowed_hosts`) get `421`, and browser
requests from a foreign `Origin` get `403` (DNS-rebinding and CSRF protection).

### `identity`

Users sign in to the proxy with Google, through the organization's own OAuth client.

```yaml
identity:
  provider: google
  client_id: ${GOOGLE_CLIENT_ID}
  client_secret: ${GOOGLE_CLIENT_SECRET}
  workspace_domain: ${GOOGLE_WORKSPACE_DOMAIN}
  # endpoints: only overridden to point at the local fake Google
```

The proxy requests exactly `openid email` plus the upstream's scopes (Gmail:
`https://www.googleapis.com/auth/gmail.readonly`), with offline access,
`include_granted_scopes=false` and PKCE. It then refuses, and revokes at Google, any grant
that is missing `gmail.readonly` or holds any other scope (write scopes such as
`gmail.send`, `gmail.compose`, `gmail.modify` and `https://mail.google.com/` are named in
the error). It checks the scope set twice: in the token response and through Google's
tokeninfo endpoint. Every refresh is checked again.

### `client_auth`

How MCP clients authenticate to the proxy. Only `personal_key` exists today (for
nexos.ai):

```yaml
client_auth:
  mode: personal_key
  api_key_headers: [x-api-key]    # accepted besides "Authorization: Bearer <key>"
  key_ttl_days: 180               # null for keys that never expire
  max_active_keys_per_user: 10
```

Keys look like `mrp_<43 random characters>` and are stored only as sha256 hashes. A
user creates and revokes them on the proxy's web page after signing in with Google.

The auth layer is a FastMCP `AuthProvider`; the policy core only sees a `Principal` built
from the access token's `sub`, `email`, `client` and `credential_id` claims
(`src/mcp_ro_proxy/auth/principal.py`). An OAuth authorization-server mode for Claude
custom connectors can be added as another provider without touching the policy core. It
is a documented follow-up, not built: on Claude, the organization can rely on Anthropic's
server-side **Blocked** tool policy for the built-in Gmail connector for now.

### `storage`

```yaml
storage:
  database_url: ${DATABASE_URL:-sqlite:///./data/gmail-ro.db}
  encryption_keys: ${TOKEN_ENCRYPTION_KEYS}
  session_secret: ${SESSION_SECRET}
```

The store keeps users (Google `sub`, email, encrypted refresh token, granted scopes) and
personal keys (hash, label, dates). It is SQLAlchemy Core, so SQLite works for local runs
and a single instance, and PostgreSQL for production (`postgresql+psycopg://...`, install
the `postgres` extra; the container image includes it). Tables are created at startup.

Refresh tokens are encrypted with AES-256-GCM, bound to the user's `sub` as associated
data. To rotate keys, put a new key first and keep the old one after it
(`k2:...,k1:...`): new writes use `k2`, old rows still decrypt, and each user's row is
re-encrypted with `k2` at their next sign-in.

### `audit`

```yaml
audit:
  log_user_email: true            # false logs only the Google sub
```

One JSON line per decision goes to stdout (Cloud Logging parses the `severity` field):

| `event` | When |
|---|---|
| `tools_list` | Every `tools/list`, with the exposed tool names |
| `tool_call` | Every `tools/call`: `tool`, `decision` (allow/deny), `reason`, `args_sha256`, `upstream_status`, `latency_ms`, `result_bytes` |
| `tool_hidden` | An allowlisted tool was hidden because its definition no longer matches the pin (`pin_mismatch`) or it disappeared upstream (`upstream_missing`). Alert on this |
| `method_denied` | A resource read or prompt get was refused |
| `sign_in`, `key_created`, `key_revoked`, `disconnect` | Web page actions |

Lines carry `user` (`sub`, `email`), `client` and `credential_id` (the key id, never the
key). They never contain tool arguments, results, message bodies or tokens. Alert on
`tool_hidden` and on `tool_call` with `decision: deny`.

### `allow`: the allowlist and pins

Default deny: a tool is exposed and callable only when it is listed here **and** the
upstream's current definition hashes to `sha256`.

```yaml
allow:
  - name: search_threads
    sha256: 29033c34...           # sha256 over name + description + input schema
    title: Search Gmail threads   # optional display title
    annotations:                  # optional; advertised instead of the upstream's
      readOnlyHint: true          # must be true (config validation rejects false)
      destructiveHint: false      # must be false
      idempotentHint: true
      openWorldHint: false
    max_response_bytes: 262144    # optional per-tool cap
```

The hash is `sha256` of the canonical JSON (sorted keys, no spaces, UTF-8) of
`{"name": ..., "description": ..., "inputSchema": ...}`. If the upstream changes a tool,
for example a read tool gains a new parameter, the tool disappears from `tools/list`,
calls to it fail as unknown, and a `tool_hidden` warning is logged until someone reviews
the change and updates the pin.

`tools/call` is checked independently of listing: allowlist, current pin, and the
arguments against the pinned input schema, before anything is forwarded. Unknown and
unlisted tools get the same "Unknown tool" error.

Capture or refresh pins with:

```sh
uv run mcp-ro-proxy pins --config config/gmail.yaml
```

It prints every upstream tool with its current hash and status (`allowlisted, pin ok`,
`PIN MISMATCH`, `not allowlisted`) and the upstream's own `readOnlyHint`. Upstream
annotations are informational only; review the description and schema yourself. For the
Gmail upstream, a test (`tests/test_cli_and_keys.py`) fails if the shipped pins stop
matching the code, so a tool change cannot ship without a reviewed pin update.
