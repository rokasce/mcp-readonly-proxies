# Configuration

The guard has two kinds of configuration:

- **`guard.yaml`** (in git): the upstreams, their allowlists, pins and limits. Policy lives here,
  reviewed like code. This page is its reference.
- **`MCP_GUARD_*` environment variables**: the deployment (which upstream, its public URL, the
  Google OAuth client, storage, secrets). Listed in the [README](../README.md#settings).

Both are strict: an unknown `MCP_GUARD_*` variable or an unknown key anywhere in `guard.yaml`
stops the guard at startup, so a typo can't quietly switch a protection off.

## `guard.yaml`

```yaml
upstreams:
  gmail:                       # MCP_GUARD_UPSTREAM picks one; one guard process serves one
    kind: gmail_api
    url: https://gmail.googleapis.com
    google_scopes:
      - https://www.googleapis.com/auth/gmail.readonly
    allow: [search_threads, get_thread, get_message, list_drafts, get_draft, list_labels]
    max_response_bytes: 1000000          # optional, this is the default
    rate_limit: {per_second: 2, burst: 20}   # optional, these are the defaults
  gmail-mcp:
    kind: mcp
    url: https://gmailmcp.googleapis.com/mcp/v1
    google_scopes: [https://www.googleapis.com/auth/gmail.readonly]
    allow: [search_threads, get_thread, get_message, list_drafts, get_draft, list_labels]
    pins:                                # optional, mcp only
      search_threads: 3f0c...            # sha256 of the reviewed definition
```

| Key | Required | Meaning |
|---|---|---|
| `kind` | yes | `gmail_api`: the guard's own read-only Gmail tools on the GA Gmail REST API at `url`. `mcp`: an MCP server at `url`, called over Streamable HTTP as the signed-in person |
| `url` | yes | https (or a local address, for tests). For `mcp` it must be a `*.googleapis.com` host: every call carries the person's Google token, which must never go to a third party |
| `google_scopes` | yes | The upstream's Google scopes, asked for at sign-in next to `openid` and `userinfo.email`. Only `.readonly` scopes are accepted. A token holding any scope beyond these is refused (and revoked, when the guard obtained it) |
| `allow` | yes | Exact tool names agents may list and call. Everything else is hidden from `tools/list` and refused on `tools/call` with "Unknown tool", without reaching the upstream. Empty, padded or duplicate names are refused |
| `pins` | no | `mcp` only. Tool name (from `allow`) to the sha256 of its definition. See [Pins](#pins) |
| `max_response_bytes` | no | Tool results over this size are cut and end with "[Response truncated due to size limit]". Default 1,000,000 |
| `rate_limit.per_second`, `rate_limit.burst` | no | Per person, across all MCP requests (tools/list included): a sustained rate with room for a burst. Over it, the request fails with "Rate limit exceeded". Defaults 2 and 20 |

Resources, prompts and resource templates of an `mcp` upstream are never offered: the allowlist
only lets tools through.

### `gmail_api` tools

All read-only, all `GET` requests to the Gmail API with the person's own `gmail.readonly` token.
Ids are checked (`[A-Za-z0-9_-]{1,128}`) before they go into a URL; unknown arguments are refused.

| Tool | Gmail API | Notes |
|---|---|---|
| `search_threads` | `threads.list`, then `threads.get` (metadata) per result | Gmail search syntax in `query`; up to 50 per page; `include_spam_trash` |
| `get_thread` | `threads.get` | Bodies as text (HTML turned into text); `include_bodies: false` for headers only |
| `get_message` | `messages.get` | |
| `list_labels` | `labels.list` | |
| `list_drafts` | `drafts.list`, then `drafts.get` (metadata) per result | Drafts can be read, never created, changed or sent |
| `get_draft` | `drafts.get` | |

Bodies are cut at 20,000 characters (or `max_body_chars`, if smaller). Attachments are listed by
name, type and size; their content isn't fetched.

The names match the tools Google's Gmail MCP server marks read-only, so `allow` is the same for
both upstreams (a test checks this against `tests/fixtures/gmail_tools.json`).

### Pins

An allowlisted name says nothing about what the tool does. If an `mcp` upstream changes a tool's
description or input schema (say a read tool gains a `send` flag), a pin catches it: the guard
hashes the tool's name, description and input schema (canonical JSON, sha256), and a pinned tool
whose hash no longer matches is dropped from `tools/list` and refused on `tools/call`, with a
warning:

```
upstream=gmail-mcp tool=search_threads dropped: its definition changed (pinned 3f0c..., now 9a1b...)
```

To pin a tool: review its definition (as an MCP client lists it), pin it to any 64-hex value, list
tools once, and copy the `now` hash from the warning. When a pinned tool drops out later, review
what changed before updating the pin. `gmail_api` tools are the guard's own code, so they take no
pins.

## Audit log

After every tool call the guard writes one JSON object per line to stdout (logger
`mcp_guard.audit`):

```json
{"event": "tool_call", "upstream": "gmail", "person": "pat@example.com", "client": "oauth",
 "tool": "search_threads", "decision": "allow", "outcome": "ok", "duration_ms": 21}
```

| Field | Values |
|---|---|
| `person` | The signed-in person's Google email |
| `client` | `oauth` (Claude and other MCP OAuth clients), `personal_key` (nexos.ai and other API-key clients), `dev` (a token pasted in local test mode) |
| `decision` | `allow`, or `deny` when the call was refused or rate-limited |
| `outcome` | `ok`; `error` (the upstream answered with an error, or couldn't be reached); `refused` (the tool isn't allowed, or its pinned definition changed); `rate_limited` |
| `duration_ms` | How long the call took in the guard, upstream included |

Arguments (search queries, ids) and results (mail) are never logged. People refused by the domain
check are logged as a warning (`refused person=... not a verified example.com account`) and never
reach the audit line. Other log lines are plain text on stderr.
