# mcp-readonly-proxies

Read-only access control for MCP connectors (Gmail, Payhawk) in Claude and nexos.ai: research findings, admin runbooks and, later, allowlisting MCP proxies.

> **Status:** this repo currently holds **findings and runbooks only**. The proxy code is **not built yet**. Whether and how to build it is still being decided (see [docs/open-questions.md](docs/open-questions.md)). The design in [docs/proxy-design.md](docs/proxy-design.md) is a proposal.

All sources were checked on **2026-11-08**.

## Bottom line

On both Claude **Team** and **Enterprise**, the built-in Gmail connector can be made read-only for the whole org: an Owner sets its write tools (or the whole write/delete group) to **Blocked**, Anthropic enforces this on its servers across claude.ai web, Desktop, mobile and Claude Code, and members cannot override it. But this is **not a read-only credential**: the connector's Google grant still holds send permission (`gmail.compose`), so the guarantee lives entirely in Anthropic's policy layer. A **credential-level guarantee** needs an OAuth token that holds only `gmail.readonly`, which you can only get with an **OAuth client you own** (Google's own Gmail MCP server, or your own read-only proxy), with a Google Workspace admin blocking Anthropic's "Claude for Gmail" app. **nexos.ai** has only whole-integration Active / Inactive; the only enforced read-only Gmail there is a read-only MCP server added as a custom MCP. Payhawk's official MCP can be write-blocked in Claude by the same org tool policy; a self-hosted Payhawk proxy needs Payhawk Support to allowlist its redirect URI.

## Comparison of solutions

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

## Documents

| Document | What it covers | Audience |
|---|---|---|
| [docs/findings.md](docs/findings.md) | Full research findings with references and evidence labels | Anyone deciding |
| [docs/proxy-design.md](docs/proxy-design.md) | Proposed design for a generic default-deny allowlisting proxy (Gmail, Payhawk) | Engineers; pending decisions |
| [docs/open-questions.md](docs/open-questions.md) | Decisions that would change the recommendation | Decision makers |
| [docs/runbooks/google-workspace-admin.md](docs/runbooks/google-workspace-admin.md) | Every step that needs a Google Workspace admin | Workspace admins |
| [docs/runbooks/claude-org.md](docs/runbooks/claude-org.md) | Claude Owner steps on Team and Enterprise | Claude Owners |
| [docs/runbooks/claude-code-managed-settings.md](docs/runbooks/claude-code-managed-settings.md) | Claude Code managed MCP allowlist / denylist, deny rules, hooks | IT, Claude Owners |
| [docs/runbooks/nexos.md](docs/runbooks/nexos.md) | nexos.ai Owner steps | nexos Owners |
| [docs/runbooks/payhawk.md](docs/runbooks/payhawk.md) | Payhawk MCP options, tool classification, Support request | Claude Owners, finance admins |

## Evidence labels

- **Verified** = stated in official vendor docs (link given).
- **Community** = observed by users in public bug reports (GitHub issues), not stated by the vendor.
- **Inferred** = reasoning from the docs; not stated anywhere. Needs a test before you rely on it.
