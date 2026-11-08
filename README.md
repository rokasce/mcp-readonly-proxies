# MCP Guard

MCP Guard gives Claude (Claude.ai, Desktop, mobile, Claude Code) and nexos.ai read-only Gmail.
Agents connect to the guard instead of Gmail and see only the read-only tools allowed in
`guard.yaml`. Every other tool, including any Gmail adds later, is hidden and refused.
[Research](#research) explains why a proxy is needed.

## How it works

```
Claude / nexos.ai  --MCP-->  MCP Guard  --person's gmail.readonly token-->  Gmail
                             (allowlist, domain check, audit log, limits)
```

- **Own mailbox only.** Each person signs in with Google through the guard, which calls Gmail with
  their own token.
- **Read-only at Google.** Sign-in asks only for `gmail.readonly`. A token holding more
  (`gmail.modify`, `https://mail.google.com/`, ...) is refused and revoked, so Google refuses
  changes even if a tool were allowed by mistake.
- **One company.** Only verified accounts of the company's Workspace domain get through.
- **No attachments.** The guard lists each attachment's name, type and size, but never reads
  its contents. A long body is returned in parts (`body_offset`), 20,000 characters at a time.
- **Audit.** One JSON line per tool call: who, which tool, allowed or not, outcome, duration.
  Arguments and mail are never logged.
- **Allowed tools:** `search_threads`, `get_thread`, `get_message`, `list_drafts`, `get_draft`,
  `list_labels`. Everything else (sending, creating drafts or labels, labelling, trash, spam) is
  blocked.

**Claude:** add `https://<guard>/mcp` as a connector, then sign in with Google. **nexos.ai:** each
person makes a personal API key on the guard's `/keys` page ([docs/nexos-setup.md](docs/nexos-setup.md)).

## Gmail API or Gmail MCP

| `kind` in `guard.yaml` | Upstream | Status |
|---|---|---|
| `gmail_api` (**default**, upstream `gmail`) | [Gmail REST API](https://developers.google.com/workspace/gmail/api/reference/rest) via the guard's own six tools | Generally available |
| `mcp` (upstream `gmail-mcp`) | Google's [Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server), filtered to its read-only tools | Developer Preview |

**Use the Gmail API for now.** Gmail MCP needs preview enrolment, runs under preview terms, and
Google asks for prompt-injection screening (e.g. Model Armor) in front of it. Both use the same
`gmail.readonly` token and tool names, so agents see no difference.

**To switch** once Gmail MCP is generally available, set `MCP_GUARD_UPSTREAM=gmail-mcp` and do
this on the Google side:

1. Enable the Gmail MCP API (`gmailmcp.googleapis.com`) in the OAuth client's Cloud project.
2. Tick **Use this client for an AI-powered agent** on the OAuth client (Google Auth Platform >
   Clients). If the client can't take it, create a new one and update the client ID and secret.
3. Check Google's terms then. In preview: enrolment, and screening prompts and responses for
   prompt injection (Model Armor or your own documented solution).
4. Compare the live `tools/list` with the allowlist. Google's public reference lists fewer tools
   than `tests/fixtures/gmail_tools.json` (`get_draft` is missing).
5. Pin the tool definitions ([`pins`](docs/configuration.md#pins)) so a changed tool is dropped
   until reviewed.

## Settings

`MCP_GUARD_UPSTREAM` is always required; the next three rows are required outside `--dev`.

| Variable | What it sets |
|---|---|
| `MCP_GUARD_UPSTREAM` | Upstream from `guard.yaml`: `gmail` or `gmail-mcp` |
| `MCP_GUARD_BASE_URL` | The guard's public https URL; Google redirects back to it |
| `MCP_GUARD_GOOGLE_CLIENT_ID`, `MCP_GUARD_GOOGLE_CLIENT_SECRET` | Google OAuth client, see [DEPLOY.md](DEPLOY.md#1-google-oauth-client-needed-for-any-deployment) |
| `MCP_GUARD_ALLOWED_DOMAIN` | e.g. `example.com`; must equal the account's Google `hd` claim |
| `MCP_GUARD_JWT_SIGNING_KEY` | Signs the guard's tokens. Default: derived from the client secret, so rotating that secret signs everyone out |
| `MCP_GUARD_CLIENT_REDIRECT_URIS` | Comma-separated callbacks MCP clients may register; replaces the default, which is Claude only (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, `http://localhost:*/callback`, `http://127.0.0.1:*/callback`) |
| `MCP_GUARD_PERSONAL_KEYS` | `true` serves `/keys` and accepts personal keys on `/mcp`. Default `false` |
| `MCP_GUARD_STORAGE_URL` | Sign-in and key storage: `redis://` or `rediss://` (for more than one replica), `file:///path`, or `memory://` (lost on restart). Default: files under `FASTMCP_HOME` |
| `MCP_GUARD_STORAGE_ENCRYPTION_KEYS` | Comma-separated Fernet keys; the first encrypts, all decrypt. Default: derived from the JWT signing key |
| `MCP_GUARD_CONFIG` | Default `guard.yaml` |
| `MCP_GUARD_HOST`, `MCP_GUARD_PORT` | Default `0.0.0.0:3000` (`127.0.0.1` with `--dev`) |
| `FASTMCP_HOME` | State directory when no storage URL is set; must be writable and persistent |

An unknown `MCP_GUARD_*` variable or a bad `guard.yaml` stops the guard at startup
([docs/configuration.md](docs/configuration.md)).

## Local test mode (no OAuth client)

`--dev` skips the sign-in: each request carries a Google token in `Authorization: Bearer`, which
the guard checks with Google. It needs `gmail.readonly` and `userinfo.email` (`openid` optional);
any other scope gets it refused. It listens on 127.0.0.1 only.

1. In [OAuth Playground](https://developers.google.com/oauthplayground), authorize
   `https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email`
   and exchange the code for tokens. Copy the access token (valid 1 hour).
2. Run `uv run python scripts/playground_check.py --domain example.com` (no `--domain` for a
   personal account). It checks the six tools work and write tools are refused.
3. Or use it from Claude Code:
   `MCP_GUARD_UPSTREAM=gmail MCP_GUARD_ALLOWED_DOMAIN=example.com uv run mcp-guard --dev`, then
   `claude mcp add --transport http gmail http://localhost:3000/mcp --header "Authorization: Bearer <token>"`

A deployment never uses Playground ([DEPLOY.md](DEPLOY.md#scopes)). To try the full sign-in
without an OAuth client, use the fake Google ([docs/local-development.md](docs/local-development.md)).

## Run and test

```bash
uv sync
uv run pytest          # fake Google, Gmail API and Gmail MCP; no Google needed
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run mcp-guard --port 3000
curl localhost:3000/health   # {"status":"ok","upstream":"gmail"}
docker build -t mcp-guard .  # runs the tests in its test stage
```

## Status

- **Tried:** `--dev` against real Gmail (`scripts/playground_check.py` passed); the full sign-in
  against a fake Google, end to end with Claude Code and a personal key.
- **Not yet:** deployed or tried with real Google sign-in ([DEPLOY.md](DEPLOY.md)).
- **Before rollout:** the decisions and questions for leadership in
  [docs/questions.md](docs/questions.md), and the risks in
  [DEPLOY.md, Things to keep in mind](DEPLOY.md#things-to-keep-in-mind).
- **Not built:** Payhawk ([docs/runbooks/payhawk.md](docs/runbooks/payhawk.md)), and argument
  checks. The allowlist is by tool name, enough while every allowed tool is read-only whatever its
  arguments.

## Research

How to restrict MCP tools in Claude and nexos.ai (checked 2026-11-08):

- **Claude Team and Enterprise:** an Owner can block the built-in Gmail connector's write tools
  on every Claude surface. But its Google grant can still send (`gmail.compose`), so read-only
  rests on Anthropic's policy, not the credential.
- **A credential-level guarantee** needs a token with only `gmail.readonly`, so an OAuth client
  you own (this guard), with the Workspace admin blocking Anthropic's "Claude for Gmail" app.
- **On Team**, a member's personal Claude account can still connect work Gmail until "Claude for
  Gmail" is blocked at Google. Our plan is believed to be Team, pending confirmation.
- **nexos.ai** can only turn an integration on or off, so read-only Gmail needs a custom MCP like
  this guard.

Full research, the options compared and the evidence: [docs/findings.md](docs/findings.md).

## Documents

| Document | What it covers |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Deploying and connecting the guard |
| [docs/configuration.md](docs/configuration.md) | `guard.yaml`: upstreams, allowlist, pins, limits, audit line |
| [docs/local-development.md](docs/local-development.md) | Tests, fake Google, full sign-in locally |
| [docs/nexos-setup.md](docs/nexos-setup.md) | nexos.ai: Owner setup and personal keys |
| [docs/security-model.md](docs/security-model.md) | Guarantees, threats, what isn't verified |
| [docs/proxy-design.md](docs/proxy-design.md) | Design, auth flows, build vs buy |
| [docs/findings.md](docs/findings.md) | Full research with references |
| [docs/questions.md](docs/questions.md) | Decisions and questions for leadership |
| Runbooks | Admin steps: [Google Workspace](docs/runbooks/google-workspace-admin.md), [Claude org](docs/runbooks/claude-org.md), [Claude Code managed settings](docs/runbooks/claude-code-managed-settings.md), [Payhawk](docs/runbooks/payhawk.md) |

All of these are also on one page: [docs/site/index.html](docs/site/index.html) (generated by
`uv run docs/site/build.py`).
