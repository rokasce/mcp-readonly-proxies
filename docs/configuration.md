# Configuration

Two places, both strict: an unknown key or `MCP_GUARD_*` variable stops the guard at startup.

- **`guard.yaml`** (in git): upstreams, allowlists, pins, limits. Reviewed like code. This page is
  its reference.
- **`MCP_GUARD_*` environment variables**: the deployment. See the [README](../README.md#settings).

## `guard.yaml`

```yaml
upstreams:
  gmail-mcp:                   # MCP_GUARD_UPSTREAM picks one; one guard process serves one
    kind: mcp
    url: https://gmailmcp.googleapis.com/mcp/v1
    google_scopes: [https://www.googleapis.com/auth/gmail.readonly]
    allow: [search_threads, get_thread, get_message, list_drafts, get_draft, list_labels]
    pins: {search_threads: 3f0c...}           # optional, mcp only
    max_response_bytes: 1000000               # optional, default
    rate_limit: {per_second: 2, burst: 20}    # optional, defaults
```

The repo's `guard.yaml` defines `gmail` (the default, `kind: gmail_api`) and
`gmail-mcp`.

| Key | Meaning |
|---|---|
| `kind` | `gmail_api` (the guard's own tools on the Gmail REST API) or `mcp` (an MCP server) |
| `url` | https (or a local address, for tests). Both kinds need a `*.googleapis.com` host, since calls carry the person's Google token |
| `google_scopes` | Asked for at sign-in, next to `openid` and `userinfo.email`. Only `.readonly` scopes are accepted. A token holding more is refused, and revoked if the guard obtained it |
| `allow` | Exact tool names agents may list and call. Others are hidden and refused ("Unknown tool") |
| `pins` | Optional, `mcp` only. See [Pins](#pins) |
| `max_response_bytes` | Optional. Longer results are cut, ending in "[Response truncated due to size limit]". Default 1,000,000 |
| `rate_limit` | Optional. Per person, all MCP requests: `per_second` (default 2), `burst` (default 20) |

`kind`, `url`, `google_scopes` and `allow` are required. An `mcp` upstream's resources and prompts
are never offered.

### `gmail_api` tools

Each tool only sends `GET`s to the Gmail API with the person's token. Ids and arguments are
checked.

| Tool | Gmail API |
|---|---|
| `search_threads` | `threads.list` (Gmail search syntax, up to 50 per page) |
| `get_thread` | `threads.get`, bodies as text; `include_bodies: false` for headers only |
| `get_message` | `messages.get` |
| `list_labels` | `labels.list` |
| `list_drafts` | `drafts.list` |
| `get_draft` | `drafts.get` |

Bodies are cut at 20,000 characters; attachments are listed, never fetched. The names match
Gmail MCP's read-only tools, so `allow` is the same for both upstreams.

### Pins

A pin is the sha256 of a tool's name, description and input schema. If an `mcp` upstream changes
a pinned tool (say a read tool gains a `send` flag), the guard hides it, refuses calls, and logs:

```
upstream=gmail-mcp tool=search_threads dropped: its definition changed (pinned 3f0c..., now 9a1b...)
```

To pin a tool:

1. Review its definition as an MCP client lists it.
2. Pin it to any 64-character lowercase hex value and list tools once.
3. Copy the `now` hash from the warning into `pins`.

When a pinned tool drops out later, review the change before updating the pin. `gmail_api` tools
take no pins.

## Audit log

One JSON line per tool call on stdout (logger `mcp_guard.audit`). Other logs are plain text on
stderr.

```json
{"event": "tool_call", "upstream": "gmail", "person": "pat@example.com", "client": "oauth",
 "tool": "search_threads", "decision": "allow", "outcome": "ok", "duration_ms": 21}
```

| Field | Values |
|---|---|
| `person` | The signed-in person's Google email |
| `client` | `oauth` (Claude), `personal_key` (nexos.ai), `dev` (a token pasted in local test mode) |
| `decision` | `allow`, or `deny` when refused or rate-limited |
| `outcome` | `ok`, `error` (upstream error or unreachable), `refused` (not allowed, or pin changed), `rate_limited` |
| `duration_ms` | Time in the guard, upstream included |

Arguments and results (mail) are never logged. People outside the domain get no audit line, only
a warning: `refused person=...: not a verified example.com account`.
