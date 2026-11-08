# MCP Guard

A proxy in front of Gmail (and other MCP servers that sign people in with Google). Agents connect
to the guard instead of the upstream, and see and call only the read-only tools allowed in
`guard.yaml`. Every other tool, including any the upstream adds later, is left out of `tools/list`
and refused on `tools/call` without reaching the upstream. It works for Claude (Team and
Enterprise: claude.ai, Desktop, mobile, Claude Code) and for nexos.ai.

This repo also holds the research behind it: how to restrict MCP tools in Claude and nexos.ai,
with admin runbooks ([Background](#background-restricting-mcp-tools-in-claude-and-nexosai)).

## Gmail

| Allowed (read-only) | Blocked (everything else) |
|---|---|
| `search_threads`, `get_thread`, `get_message`, `list_drafts`, `get_draft`, `list_labels` | e.g. sending, `create_draft`, `create_label`, label/unlabel, trash/untrash, spam and sensitive-label tools |

Each person signs in with Google through the guard (it is an OAuth proxy to Google). The guard
calls Gmail with that person's own token, refreshing it as needed, so each person reaches only
their own mailbox. Sign-in asks only for `gmail.readonly`, and a Google token holding anything more
(`gmail.modify`, `https://mail.google.com/`, ...) is refused and revoked at Google, so Google
itself refuses changes even if a tool were allowed by mistake. Only verified accounts of the
company's Google Workspace domain get through.

To use it in Claude: add the guard's URL once (`claude mcp add --transport http gmail
https://<guard>/mcp`, or as a custom connector in Claude.ai), then sign in with Google when asked.
For nexos.ai, whose custom MCP takes a per-person API key, each person makes a **personal key** on
the guard's `/keys` page ([docs/nexos-setup.md](docs/nexos-setup.md)).

After every tool call the guard writes one JSON line: who, which tool, whether it was allowed, how
it ended and how long it took. Arguments (search queries, ids) and mail are never logged.

## Gmail API or Gmail MCP

The guard can reach Gmail two ways, both behind the same allowlist, domain check, audit and limits:

| `kind` in `guard.yaml` | Upstream | Status |
|---|---|---|
| `gmail_api` (**default**, upstream `gmail`) | The GA [Gmail REST API](https://developers.google.com/workspace/gmail/api/reference/rest), through the guard's own six read-only tools | Generally available |
| `mcp` (upstream `gmail-mcp`) | Google's [Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) (`https://gmailmcp.googleapis.com/mcp/v1`), filtered to the tools it marks read-only | Developer Preview |

Use the Gmail API for now. Google's Gmail MCP is in Developer Preview: it needs enrolment in the
preview program, runs under preview terms, and Google asks for prompt-injection screening (e.g.
Model Armor) in front of its Workspace MCP servers. The Gmail API has none of that. Both use the
same `gmail.readonly` token, so the read-only floor is the same, and the guard's tools have the same
names as Gmail MCP's read-only ones, so agents see no difference.

To switch once Gmail MCP is generally available, set `MCP_GUARD_UPSTREAM=gmail-mcp` (the entry is
already in `guard.yaml`); nothing else changes, not even the OAuth client. Optionally pin its tool
definitions (`pins`, [docs/configuration.md](docs/configuration.md#pins)) so a changed tool is
dropped until reviewed.

## Settings

| Variable | |
|---|---|
| `MCP_GUARD_UPSTREAM` | Which upstream in `guard.yaml` this process fronts: `gmail` (Gmail API) or `gmail-mcp` |
| `MCP_GUARD_BASE_URL` | The guard's public https URL; Google redirects back to it after sign-in |
| `MCP_GUARD_GOOGLE_CLIENT_ID`, `MCP_GUARD_GOOGLE_CLIENT_SECRET` | A Google OAuth client (Web application, **Internal** to the Workspace) with redirect URIs `<base url>/auth/callback` and, with personal keys, `<base url>/keys/callback` |
| `MCP_GUARD_ALLOWED_DOMAIN` | e.g. `example.com`: only verified accounts of that Google Workspace domain get through. Google's `hd` claim must equal it, so a personal Google account for a company address is refused too |
| `MCP_GUARD_JWT_SIGNING_KEY` (optional) | Signs the guard's own tokens. Left out, it's derived from the client secret, so rotating that secret signs everyone out |
| `MCP_GUARD_CLIENT_REDIRECT_URIS` (optional) | Comma-separated callback patterns MCP clients may register. Default, Claude only: `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, `http://localhost:*/callback`, `http://127.0.0.1:*/callback`. Setting it replaces the default |
| `MCP_GUARD_PERSONAL_KEYS` (optional) | `true` serves the personal-key page (`/keys`) and accepts those keys on `/mcp`, next to the OAuth sign-in. Default `false` |
| `MCP_GUARD_STORAGE_URL` (optional) | Where sign-ins and personal keys are kept: `redis://...` / `rediss://...` (needed for more than one replica), `file:///path`, or `memory://` (lost on restart). Left out: files under `FASTMCP_HOME` |
| `MCP_GUARD_STORAGE_ENCRYPTION_KEYS` (optional) | Comma-separated Fernet keys that encrypt everything stored; the first encrypts, all decrypt, so rotation is: add the new key first, drop the old one later. Left out, one key is derived from the JWT signing key |
| `MCP_GUARD_CONFIG` (optional) | Path to the config, default `guard.yaml` |
| `MCP_GUARD_HOST`, `MCP_GUARD_PORT` (optional) | Where it listens; default `0.0.0.0:3000` (`127.0.0.1` with `--dev`) |
| `FASTMCP_HOME` (optional) | Where sign-in state goes when `MCP_GUARD_STORAGE_URL` isn't set. Must be writable and survive restarts, or everyone signs in again |

Sign-in shows the guard's own approval page ("Claude Code wants to access gmail-guard") the first
time a client signs in from a browser, then remembers it; cross-site navigations are still asked.
Any client whose callback isn't allowed is refused when it registers.

Any other `MCP_GUARD_*` variable is refused at startup, so a misspelt optional setting can't go
unnoticed. `guard.yaml` is checked the same way: unknown keys, duplicate or padded tool names, a
non-https upstream, a Google scope that isn't `.readonly`, and an `mcp` upstream outside
`googleapis.com` (it would receive people's Google tokens) are refused. The keys of `guard.yaml`
are in [docs/configuration.md](docs/configuration.md).

## Local test mode (no OAuth client)

`--dev` skips the sign-in: each request brings a Google token in `Authorization: Bearer`, which
the guard checks with Google. The token must have `gmail.readonly` and `userinfo.email`; `openid`
is optional, and any other scope gets the token refused (e.g. `https://mail.google.com/` or
`gmail.modify`, so a full-access Gmail token isn't trusted). It listens on 127.0.0.1 only.
OAuth Playground is only for this mode; a deployment runs its own sign-in (see
[DEPLOY.md](DEPLOY.md#scopes)).

1. In [OAuth Playground](https://developers.google.com/oauthplayground), enter the scopes
   `https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email`,
   authorize with your account, then "Exchange authorization code for tokens"; copy the access
   token (valid 1 hour).
2. Check the guard with it in one go: `uv run python scripts/playground_check.py --domain example.com`
   (leave out `--domain` for a personal Google account). It asks for the token without echoing it,
   starts the guard in `--dev`, and checks that the token is accepted, the six tools work and
   write tools are refused. Given a token with more access (add `gmail.modify` in step 1), it checks
   the guard refuses it. The token goes only to Google and to the guard on 127.0.0.1; mail shows
   up only as the subjects of your three most recent threads.
3. Or run the guard and use it from Claude Code:
   `MCP_GUARD_UPSTREAM=gmail MCP_GUARD_ALLOWED_DOMAIN=example.com uv run mcp-guard --dev`, then
   `claude mcp add --transport http gmail http://localhost:3000/mcp --header "Authorization: Bearer <token>"`

To try the full deployed sign-in (Claude Code's OAuth flow, the personal-key page) without any
Google OAuth client, run the guard against a fake Google:
`uv run python -m tests.fakes.run_local` ([docs/local-development.md](docs/local-development.md)).

## Run and test

```bash
uv sync
uv run pytest          # fake Google, Gmail API and Gmail MCP; no Google needed
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run mcp-guard --port 3000
curl localhost:3000/health   # {"status":"ok","upstream":"gmail"}; no sign-in, doesn't call the upstream
docker build -t mcp-guard .  # runs the tests in its test stage
```

If the upstream can't be reached, agents get "Couldn't reach gmail … Try again later." instead of
an empty tool list, and every call upstream gives up after 60 s.

`tests/fixtures/gmail_tools.json` is Gmail MCP's tool list. A test checks both Gmail allowlists
equal the tools Gmail MCP marks read-only, and that the guard's own Gmail API tools are exactly
those; refresh the snapshot when Gmail changes its tools.

## Status

Runs locally, in `--dev` against real Google and Gmail (`scripts/playground_check.py` passed with
a real OAuth Playground token: real mail read, write tools refused, a token without
`gmail.readonly` refused) and with the full sign-in against a fake Google;
tried end to end with Claude Code and with a personal key over Streamable HTTP. Not yet deployed
or tried against real Google sign-in, which needs the Google OAuth client
([DEPLOY.md](DEPLOY.md)). Payhawk isn't wired: an `mcp` upstream receives the person's Google
token, and Payhawk needs its own per-person sign-in ([docs/runbooks/payhawk.md](docs/runbooks/payhawk.md)).

Not built: **argument checks.** The allowlist is by tool name, which is enough while every allowed
tool is read-only whatever its arguments. A tool whose arguments make it dangerous (say a `delete`
flag) would need a middleware that checks arguments, not just the name.

## Background: restricting MCP tools in Claude and nexos.ai

The research this guard came from: what each product can enforce on its own, and where a proxy is
needed. All sources were checked on **2026-11-08**.

### Bottom line

On both Claude **Team** and **Enterprise**, the built-in Gmail connector can be made read-only for the whole org: an Owner sets its write tools (or the whole write/delete group) to **Blocked**, Anthropic enforces this on its servers across claude.ai web, Desktop, mobile and Claude Code, and members cannot override it. But this is **not a read-only credential**: the connector's Google grant still holds send permission (`gmail.compose`), so the guarantee lives entirely in Anthropic's policy layer. A **credential-level guarantee** needs an OAuth token that holds only `gmail.readonly`, which you can only get with an **OAuth client you own** (Google's own Gmail MCP server, or your own read-only proxy), with a Google Workspace admin blocking Anthropic's "Claude for Gmail" app. **nexos.ai** has only whole-integration Active / Inactive; the only enforced read-only Gmail there is a read-only MCP server added as a custom MCP. Payhawk's official MCP can be write-blocked in Claude by the same org tool policy; a self-hosted Payhawk proxy needs Payhawk Support to allowlist its redirect URI.

**Our plan:** believed to be **Claude Team** (from the onboarding email), pending confirmation (see [stakeholder question 1](docs/questions-for-stakeholders.md#claude-admins-owners)).

**Team-plan gap:** on Team there is no verified-domain restriction, so a member's personal Claude account can connect work Gmail outside the org policy. Closing that requires the Google Workspace admin to block "Claude for Gmail" at Google [GWA] ([runbook](docs/runbooks/google-workspace-admin.md#2-set-the-access-level-for-claude-for-gmail)), with Gmail then reached only through the read-only proxy added by an Owner as a custom connector.

### Comparison of solutions

"Enforced" means a technical control rejects the action. "Advisory" means it relies on a person clicking correctly, or on a hint. "Bypassable" means a normal member can get around it.

| # | Solution | Surfaces / plans | Enforced? | Bypassable by a member? | Effort | Reference |
|---|---|---|---|---|---|---|
| 1 | **Claude org-wide tool policy:** set Gmail write tools to **Blocked** | claude.ai web, Desktop (chat, Cowork, Code tab), mobile, Claude Code (connectors fetched from claude.ai); **Team + Enterprise** | **Enforced by Anthropic server-side.** Not a credential limit: the Google token can still send | No, members can't override. Escape routes: a personal Claude account (Team), or another client using its own Gmail access | Low | [Use connectors](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities), [Custom roles](https://support.claude.com/en/articles/13930452-manage-custom-roles-on-enterprise-plans), [Claude Code MCP](https://code.claude.com/docs/en/mcp.md#organization-controls-on-connector-tools) |
| 2 | **Enterprise custom roles:** connector and per-tool grants (Always allow / Needs approval / Blocked) | Same surfaces; **Enterprise only** | Enforced server-side. The org policy (row 1) is the ceiling. Grants add up across roles | No | Low-Med | [Custom roles](https://support.claude.com/en/articles/13930452-manage-custom-roles-on-enterprise-plans) |
| 3 | Claude tool set to **Needs approval** (org or role) | Same | **Advisory** (a human must click). Research mode calls tools "without further approval". Community reports say approvals were skipped in Claude Code | Member can approve | Low | [Custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), GitHub [#89304](https://github.com/anthropics/claude-code/issues/89304) |
| 4 | Member's own per-chat connector toggle and personal tool permissions | All plans | Advisory | Yes | None | [Get started with connectors](https://claude.com/docs/connectors/getting-started.md) |
| 5 | **Claude Code managed permission deny rules**, e.g. `mcp__claude_ai_Gmail__send_message` | Claude Code CLI, IDE, Agent SDK; server-managed settings on **Team + Enterprise**; MDM file on any plan | Enforced by the client | With MDM: no. Server-managed only: yes, by switching auth to an API key or gateway. Does **not** match in Desktop local sessions (community #92787) | Low | [Permissions](https://code.claude.com/docs/en/permissions.md), [Server-managed settings](https://code.claude.com/docs/en/server-managed-settings.md) |
| 6 | **Claude Code managed MCP allowlist / denylist**: `allowManagedMcpServersOnly`, `allowedMcpServers`, `deniedMcpServers`, `managed-mcp.json` | Claude Code | Enforced by the client | With MDM: no. Server-managed only: same escape as row 5 | Low | [Managed MCP](https://code.claude.com/docs/en/managed-mcp.md) |
| 7 | **Claude Code managed PreToolUse hook** that denies `mcp__.*__(send\|reply\|forward\|trash\|...)`, plus `allowManagedHooksOnly` | Claude Code | Enforced by the client | With MDM: no | Med | [Permissions: hooks](https://code.claude.com/docs/en/permissions.md#extend-permissions-with-hooks), [Settings reference](https://code.claude.com/docs/en/settings-reference.md) |
| 8 | **Claude Desktop policies**: `isLocalDevMcpEnabled=false`, block or allowlist desktop extensions, `forceLoginOrgUUID` | Claude Desktop; Team via MDM; Enterprise via MDM or Organization settings | Enforced by the client | With MDM: no | Low | [Desktop enterprise config](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop), [Local MCP](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop) |
| 9 | Claude API MCP connector `mcp_toolset` allowlist (`default_config.enabled:false` + `configs`); Agent SDK `allowed_tools` / `disallowed_tools` / `canUseTool` | Claude API, Agent SDK | Enforced by API and your code | Developer-controlled | Low | [MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector.md), [SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions.md) |
| 10 | **nexos.ai**: integration Active / Inactive | nexos Workspace, Chat and Agents | Enforced (whole integration only) | No | Low | [Workspace Integrations](https://docs.nexos.ai/workspace/integrations.md) |
| 11 | nexos.ai Human-in-the-Loop approvals | nexos Agents (changelog says chat too) | Advisory | Yes ("Always approve", per-agent toggle) | Low | [HITL](https://docs.nexos.ai/workspace/agents/human-in-the-loop-hitl-approvals.md) |
| 12 | **OAuth token with only `gmail.readonly`** (requires your own OAuth client) | Any client | **Hard: Google rejects writes** | No | Med | [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) |
| 13 | **Google Workspace API controls** [GWA]: per app Trusted / Limited / **Specific Google data** / **Blocked**; Gmail service Restricted; unconfigured-app policy | Any client that uses Google OAuth; any Workspace edition with API controls | **Hard (Google)** | No | Low for the admin | [Control which apps access Workspace data](https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data) |
| 14 | Service account with domain-wide delegation, scoped to `gmail.readonly` [GWA, super admin] | Server-side apps | Hard scope floor, but can **impersonate any user** | No | Med | [Domain-wide delegation](https://support.google.com/a/answer/162106) |
| 15 | **Google's Gmail MCP server** (Developer Preview) as a Claude custom connector, with your own Internal OAuth client | Claude web/desktop custom connector, Claude Code; nexos unlikely (its custom MCP auth is API keys) | Hard **if** the token is readonly-only (needs a test); plus Claude tool policy | No | Med | [Configure Gmail MCP](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) |
| 16 | **Custom read-only proxy / MCP server** ([proxy design](docs/proxy-design.md)) | Claude web/desktop, Claude Code, nexos, anything that speaks MCP | **Hard**: tools not exposed, default-deny, and `gmail.readonly` token | No, once the built-in connector is blocked | Med-High | [docs/proxy-design.md](docs/proxy-design.md) |
| 17 | MCP tool annotations (`readOnlyHint`, `destructiveHint`) | All | **Advisory hints only.** The spec says clients MUST treat them as untrusted | n/a | None | [MCP tools spec](https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md) |
| 18 | **Cloudflare MCP server portals** (managed gateway) with `default_disabled: true` allowlist and per-user upstream OAuth (own OAuth client, chosen scopes) | Any MCP client that does OAuth (Claude setup is documented); needs Cloudflare Zero Trust and a domain on Cloudflare | Enforced at the gateway: "Turned-off tools will not appear ... Users will not be able to call them". Upstream scope floor if the upstream OAuth requests only `gmail.readonly` | No | Med (configuration, no code) | [MCP portals](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/) |
| 19 | Other gateways (agentgateway, ContextForge, Docker MCP Gateway) | Depends | Enforced at the gateway | No | Med-High | [Build vs buy](docs/proxy-design.md#reuse-an-existing-gateway-or-build) |

[GWA] marks a step that needs a Google Workspace admin; see [the Workspace admin runbook](docs/runbooks/google-workspace-admin.md).

### Documents

| Document | What it covers | Audience |
|---|---|---|
| [DEPLOY.md](DEPLOY.md) | Deploying the guard: Google OAuth client, settings, hosting, storage, connecting Claude and nexos, checks, open items | Engineers, Workspace admins |
| [docs/configuration.md](docs/configuration.md) | `guard.yaml` reference: upstream kinds, allowlist, pins, limits; the audit line | Engineers |
| [docs/local-development.md](docs/local-development.md) | Tests, the fake Google, trying the full sign-in with Claude Code and personal keys | Engineers |
| [docs/nexos-setup.md](docs/nexos-setup.md) | Adding the guard to nexos.ai with personal keys | nexos Owners |
| [docs/security-model.md](docs/security-model.md) | What the guard guarantees, threats, what is not verified yet | Anyone deciding |
| [docs/proxy-design.md](docs/proxy-design.md) | Why the guard looks the way it does: design, auth flows, build vs buy | Engineers |
| [docs/findings.md](docs/findings.md) | Full research findings with references and evidence labels | Anyone deciding |
| [docs/open-questions.md](docs/open-questions.md) | Decisions that would change the recommendation | Decision makers |
| [docs/questions-for-stakeholders.md](docs/questions-for-stakeholders.md) | Questions to ask each admin group before rollout, linked to the relevant sections | Everyone involved |
| [docs/runbooks/google-workspace-admin.md](docs/runbooks/google-workspace-admin.md) | Every step that needs a Google Workspace admin | Workspace admins |
| [docs/runbooks/claude-org.md](docs/runbooks/claude-org.md) | Claude Owner steps on Team and Enterprise | Claude Owners |
| [docs/runbooks/claude-code-managed-settings.md](docs/runbooks/claude-code-managed-settings.md) | Claude Code managed MCP allowlist / denylist, deny rules, hooks | IT, Claude Owners |
| [docs/runbooks/nexos.md](docs/runbooks/nexos.md) | nexos.ai Owner steps | nexos Owners |
| [docs/runbooks/payhawk.md](docs/runbooks/payhawk.md) | Payhawk MCP options, tool classification, Support request | Claude Owners, finance admins |

### Reading view

[docs/site/index.html](docs/site/index.html) presents all of these documents on one page: a table of contents, a filterable and sortable comparison table, the screenshots, rendered diagrams, collapsible runbooks, light and dark mode, and a print layout. The Markdown files stay the source of truth; the HTML is generated from them.

- **Open it locally:** clone the repo and open `docs/site/index.html` in a browser (double-click it, or `xdg-open docs/site/index.html` / `open docs/site/index.html`). It needs no server; it loads Mermaid from a CDN to draw the diagrams, so be online.
- **Regenerate after editing the Markdown:** `uv run docs/site/build.py`.
- It will also be shared as a hosted page.

### Evidence labels

- **Verified** = stated in official vendor docs (link given).
- **Community** = observed by users in public bug reports (GitHub issues), not stated by the vendor.
- **Inferred** = reasoning from the docs; not stated anywhere. Needs a test before you rely on it.
