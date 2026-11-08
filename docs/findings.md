# Findings: read-only Gmail and Payhawk in Claude and nexos.ai

How to give Claude (Team or Enterprise: claude.ai, Desktop, mobile, Claude Code) and nexos.ai read-only access to Gmail and Payhawk. The guard built from this research is [MCP Guard](../README.md). Sources were checked on 2026-11-08; screenshots are from that day, highlights added.

Labels: **Verified** = in official vendor docs (linked). **Community** = seen by users in public GitHub issues. **Inferred** = our reasoning; test before relying on it. **[GWA]** = needs a Google Workspace admin ([runbook](runbooks/google-workspace-admin.md)).

## 1. Answer

**Recommendation: reach Gmail only with a Google token that holds `gmail.readonly`, issued to an OAuth client we own (MCP Guard), and block the vendors' own Gmail apps at Google.** Google then refuses every write, whatever the AI client does.

| | Gmail | Payhawk |
|---|---|---|
| **Claude** (Team or Enterprise) | An Owner adds MCP Guard as a custom connector. The built-in Gmail connector stays off. A Google admin blocks "Claude for Gmail" [GWA] | An Owner adds Payhawk's own MCP as a custom connector and sets every write tool to **Blocked**. No proxy needed |
| **Claude Code** | Managed settings allow only the guard ([4.2](#42-claude-code)) | Same org tool policy as Claude |
| **nexos.ai** | Built-in Gmail integration **Inactive**. MCP Guard added as a custom MCP, each user with a personal key | Not possible today: Payhawk accepts sign-ins only from listed apps, and nexos is not one |

Why not just the built-in Gmail connector?

- An Owner **can** set its write tools to **Blocked** on Team and Enterprise. Anthropic enforces it on its servers; members can't override it ([3.3](#33-per-tool-admin-controls-team-vs-enterprise)).
- But its Google grant still includes send permission (`gmail.compose`), so read-only rests only on Anthropic's policy layer ([8.3](#83-residual-risks-of-82-why-81-is-the-guarantee)). Anthropic's own docs disagree on whether it can send ([3.1](#31-what-the-connector-exposes)).
- If the team accepts that, it is a valid no-code option ([8.2](#82-lighter-option-built-in-connector-anthropic-enforced-read-only)).

**Team vs Enterprise:** both have the org-wide Blocked policy. Enterprise adds custom roles and a verified-domain restriction that keeps personal Claude accounts off work Gmail. On Team (our believed plan), only the Google admin blocking "Claude for Gmail" closes that gap.

**nexos.ai** can only switch a whole integration on or off. No per-tool limits; approvals are advisory.

Decisions still open: [questions.md](questions.md).

## 2. All options compared

"Enforced" means a technical control rejects the action. "Advisory" means it relies on a person clicking correctly, or on a hint. "Bypass" asks whether a normal member can get around it.

| # | Option | Where | Enforced? | Bypass? | Effort | Source |
|---|---|---|---|---|---|---|
| 1 | **Claude org tool policy:** Gmail write tools **Blocked** | All Claude surfaces; Team + Enterprise | **Yes, by Anthropic's servers.** The Google token can still send | No. Escapes: a personal Claude account (Team), other apps with Gmail access | Low | [Use connectors](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities), [Claude Code MCP](https://code.claude.com/docs/en/mcp.md#organization-controls-on-connector-tools) |
| 2 | **Enterprise custom roles:** per-role connector and tool grants | Same; Enterprise only | Yes, server-side, under the org policy | No | Low-Med | [Custom roles](https://support.claude.com/en/articles/13930452-manage-custom-roles-on-enterprise-plans) |
| 3 | Claude tool set to **Needs approval** | Same | Advisory. Research mode skips approval; reported skipped in Claude Code | Member approves | Low | [Custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), [#89304](https://github.com/anthropics/claude-code/issues/89304) |
| 4 | Member's own connector toggle and tool permissions | All plans | Advisory | Yes | None | [Getting started](https://claude.com/docs/connectors/getting-started.md) |
| 5 | **Claude Code managed deny rules** | Claude Code; server-managed (Team + Enterprise) or MDM (any plan) | Yes, by the client | With MDM, no. Server-managed only: yes, via an API key or gateway. Don't match in Desktop local sessions (#92787) | Low | [Permissions](https://code.claude.com/docs/en/permissions.md), [Server-managed settings](https://code.claude.com/docs/en/server-managed-settings.md) |
| 6 | **Claude Code managed MCP allowlist / denylist** | Claude Code | Yes, by the client | As row 5 | Low | [Managed MCP](https://code.claude.com/docs/en/managed-mcp.md) |
| 7 | **Claude Code managed `PreToolUse` hook** denying write tools, plus `allowManagedHooksOnly` | Claude Code | Yes, by the client | With MDM, no | Med | [Hooks](https://code.claude.com/docs/en/permissions.md#extend-permissions-with-hooks), [Settings](https://code.claude.com/docs/en/settings-reference.md) |
| 8 | **Claude Desktop policies:** local MCP off, extensions blocked or allowlisted, `forceLoginOrgUUID` | Desktop; MDM (Enterprise also Organization settings) | Yes, by the client | With MDM, no | Low | [Desktop config](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop), [Local MCP](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop) |
| 9 | Claude API `mcp_toolset` allowlist; Agent SDK `allowed_tools` / `canUseTool` | API, Agent SDK | Yes, by the API and your code | Developer-controlled | Low | [MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector.md), [SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions.md) |
| 10 | **nexos.ai integration Active / Inactive** | nexos | Yes, whole integration only | No | Low | [Integrations](https://docs.nexos.ai/workspace/integrations.md) |
| 11 | nexos.ai approvals (HITL) | nexos agents and chat | Advisory | Yes ("Always approve") | Low | [HITL](https://docs.nexos.ai/workspace/agents/human-in-the-loop-hitl-approvals.md) |
| 12 | **Token with only `gmail.readonly`** (needs our own OAuth client) | Any client | **Hard: Google rejects writes** | No | Med | [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) |
| 13 | **Google API controls** [GWA]: per app Trusted / Specific Google data / Blocked; Gmail Restricted | Any client using Google OAuth | **Hard (Google)** | No | Low | [App access](https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data) |
| 14 | Domain-wide delegation limited to `gmail.readonly` [GWA, super admin] | Server-side apps | Hard scope floor, but can impersonate any user | No | Med | [DWD](https://support.google.com/a/answer/162106) |
| 15 | **Google's Gmail MCP** (Developer Preview) as a custom connector with our own OAuth client | Claude, Claude Code; nexos unlikely (API keys only) | Hard **if** the token is readonly-only (untested) | No | Med | [Gmail MCP](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) |
| 16 | **Custom read-only proxy** ([MCP Guard](../README.md)) | Claude, Claude Code, nexos, any MCP client | **Hard:** default-deny tools and a `gmail.readonly` token | No, once the built-in connector is blocked | Med-High | [Design](proxy-design.md) |
| 17 | MCP tool annotations (`readOnlyHint`) | All | Advisory: the spec says treat them as untrusted | n/a | None | [MCP spec](https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md) |
| 18 | **Cloudflare MCP server portal** with an allowlist and per-user upstream OAuth | OAuth MCP clients; needs Cloudflare Zero Trust | Yes, at the gateway; scope floor if it requests only `gmail.readonly` | No | Med (no code) | [MCP portals](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/) |
| 19 | Other gateways (agentgateway, ContextForge, Docker MCP Gateway) | Depends | Yes, at the gateway | No | Med-High | [Build vs buy](proxy-design.md#reuse-an-existing-gateway-or-build) |

## 3. Can the built-in Gmail connector be restricted to read-only?

Its tools, yes (Blocked). Its Google credential, no.

### 3.1 What the connector exposes

Anthropic's docs disagree:

- The help article [Use Google Workspace connectors](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors) lists search and read, drafts, **send, reply and forward**, and label and thread management.
- The [Claude docs Gmail page](https://claude.com/docs/connectors/google/gmail.md) says: "Claude reads and searches email only; it can't create, send, or modify messages."

The help article matches the community reports below, so treat the connector as **able to write**.

![The help article lists send, reply, forward and label management](images/anthropic-gmail-capabilities.png)

*Source: [Anthropic Help Center](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors).*

Anthropic publishes no tool list. GitHub issues ([#89304](https://github.com/anthropics/claude-code/issues/89304), [#72052](https://github.com/anthropics/claude-code/issues/72052), [#47383](https://github.com/anthropics/claude-code/issues/47383)) show `send_message`, `reply`, `forward`, `create_draft`, `update_draft`, `trash_message`, `trash_thread`, `mark_message_spam`, `mark_thread_spam` and label tools. One user reports `send_message` succeeded from Claude Code. **Community.**

### 3.2 Which OAuth scopes it gets

- **Verified:** "Google's OAuth screen mentions email sending permissions" ([10166901](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors)).
- **Community:** users' Google permissions pages show "Claude for Gmail" with "Manage drafts and send emails" and "View your email messages and settings" ([#46206](https://github.com/anthropics/claude-code/issues/46206), [#55271](https://github.com/anthropics/claude-code/issues/55271)), that is `gmail.compose` and `gmail.readonly`. Send and draft work; label and trash fail without `gmail.modify`.
- **No Claude setting narrows it.** Enterprise-managed auth has a scopes step, but not for Gmail ([15537633](https://support.claude.com/en/articles/15537633-auto-connect-your-organization-s-apps-to-claude)).
- **Check our tenant [GWA]:** API controls > Manage Third-Party App Access > Accessed apps > "Claude for Gmail" shows its scopes.

![Google's OAuth screen mentions email sending permissions](images/anthropic-gmail-oauth-note.png)

*Source: [Anthropic Help Center](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors).*

What we can do about it:

- **Accept it**, relying on Anthropic's Blocked policy ([8.3](#83-residual-risks-of-82-why-81-is-the-guarantee)).
- **Limit "Claude for Gmail" to `gmail.readonly` at Google** [GWA]. It always asks for `gmail.compose` too, so sign-in will likely just fail (**Inferred**; pilot on one OU).
- **Block "Claude for Gmail" at Google** [GWA] and use an OAuth client we own. **Recommended.**
- Ask Anthropic for a read-only variant (related GitHub issues closed as stale).

### 3.3 Per-tool admin controls, Team vs Enterprise

| Control | Team | Enterprise |
|---|---|---|
| Enable or remove Gmail for the org; members still sign in to Google themselves ([11176164](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities)) | Yes | Yes |
| **Org-wide policy per tool or group: Always allow / Needs approval / Blocked.** "Individual users can't override it" ([11176164](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities)) | **Yes** | **Yes** |
| Per-role grants. "Blocked: The connector or tool is hidden. Claude can't see it or call it." ([13930452](https://support.claude.com/en/articles/13930452-manage-custom-roles-on-enterprise-plans)) | No | **Yes** |
| Restrict verified-domain connectors: personal Claude accounts can't connect work Gmail; not retroactive ([15402193](https://support.claude.com/en/articles/15402193-restrict-verified-domain-connectors-to-your-enterprise)) | No | **Yes** |
| Member connector requests; an Owner can "Decline it and block the connector" | On by default | Off by default |

![Owners can set connector tools to Always allow, Needs approval or Blocked, org-wide](images/anthropic-tool-permissions.png)

*Source: [Anthropic Help Center](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities).*

"Connector permissions are enforced on Anthropic's servers" on every Claude surface, and "Access fails toward denying, never toward granting." Locally run connectors aren't covered ([13930452](https://support.claude.com/en/articles/13930452-manage-custom-roles-on-enterprise-plans)).

**Use Blocked, not Needs approval:** Research mode calls tools "automatically without further approval" ([11175166](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)), Desktop's local Claude Code sessions get only `blocked` ([mcp.md](https://code.claude.com/docs/en/mcp.md#organization-controls-on-connector-tools)), and users report approvals not shown in Claude Code (#89304, **Community**).

**Block the whole write/delete group**, not single tools: the docs don't say what default a new tool gets (that the group catches it is **Inferred**). The grouping likely follows MCP annotations ([review criteria](https://claude.com/docs/connectors/building/review-criteria.md)), so check each tool once.

### 3.4 Setup steps for the built-in route (Claude Owner)

Add Gmail under Organization settings > Connectors, then in its **Tool permissions** set the write/delete group and every send, reply, forward, draft, trash, spam and label tool to **Blocked**. Claude Code applies this on its own. Enterprise: turn on the verified-domain restriction. [GWA] set "Claude for Gmail" to Trusted for the OUs that use it, or users may see "Access blocked". Full steps: [runbooks/claude-org.md](runbooks/claude-org.md#route-a-built-in-gmail-connector-with-write-tools-blocked).

## 4. Claude surfaces in detail

### 4.1 claude.ai web, desktop chat and mobile (Team and Enterprise)

- **Only Owners add custom connectors** (on Enterprise also custom roles with Libraries "Manage"), so members can't add an unrestricted Gmail MCP ([11175166](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).
- **Custom connectors are called from Anthropic's cloud**, so the server must be public or behind [MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview.md). OAuth client options: **"Use Claude's published identity"** (CIMD, Anthropic's recommended option), "Register automatically" (DCR), or "Use your own OAuth client" ([11176164](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities)).
- **The org tool policy covers org-added custom connectors too**, such as Payhawk or the guard.

### 4.2 Claude Code

Claude Code loads claude.ai connectors and server-managed settings only with a Team or Enterprise claude.ai login, not with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper` or a third-party provider ([mcp.md](https://code.claude.com/docs/en/mcp.md#use-mcp-servers-from-claude-ai), [server-managed settings](https://code.claude.com/docs/en/server-managed-settings.md)). nexos documents running Claude Code through its gateway that way ([nexos](https://docs.nexos.ai/gateway-api/integrations/claude-code.md)). **Only MDM or file-deployed managed settings govern those sessions.**

| Control | What it does |
|---|---|
| Org tool policy | `blocked` tools are filtered out; `ask` prompts every call ([mcp.md](https://code.claude.com/docs/en/mcp.md#organization-controls-on-connector-tools)) |
| Deny rules | e.g. `mcp__claude_ai_Gmail__*`. A deny from any level wins; managed rules can't be overridden ([permissions.md](https://code.claude.com/docs/en/permissions.md)) |
| MCP allowlist | `allowManagedMcpServersOnly` + `allowedMcpServers` by `serverUrl` (`serverName` is "not a security control"); `deniedMcpServers` always wins ([managed-mcp.md](https://code.claude.com/docs/en/managed-mcp.md)) |
| `managed-mcp.json` | Loads only listed servers and drops claude.ai connectors. MDM file only |
| OAuth scope pin | `"oauth": {"scopes": "...gmail.readonly"}` for user-added servers ([mcp.md](https://code.claude.com/docs/en/mcp.md#restrict-oauth-scopes)) |

MDM reaches every session; "Endpoint-managed settings provide stronger security guarantees."

**Known gap (Community, [#92787](https://github.com/anthropics/claude-code/issues/92787), open):** in Desktop's Code tab, local sessions name connector tools `mcp__<uuid>__send_message`, so `mcp__claude_ai_Gmail__*` rules don't match. The org `blocked` policy is the reliable control there.

Examples: [runbooks/claude-code-managed-settings.md](runbooks/claude-code-managed-settings.md).

### 4.3 Claude Desktop, API and Agent SDK

- **Desktop** ([12622667](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)): `isLocalDevMcpEnabled=false` stops user-added local MCP servers, extensions can be blocked or allowlisted, and `forceLoginOrgUUID` keeps personal accounts out. Team needs MDM.
- **API and Agent SDK:** developer-side tool allowlists (row 9 above), outside the claude.ai org policy.

## 5. nexos.ai

All **Verified** from [docs.nexos.ai](https://docs.nexos.ai/workspace/integrations.md):

- **Built-in Gmail:** members connect their own Google account; its tools and scopes are **not documented** ([Gmail](https://docs.nexos.ai/workspace/integrations/gmail.md)).
- **Org controls:** each integration is Active or Inactive. No per-tool toggle, and no team-level limit: "Apps remain managed at the company level" ([Roles](https://docs.nexos.ai/workspace/settings/organization/roles-and-permissions.md)).
- **Approvals (HITL):** agents pause before "an action that changes something in another tool", but users can pick "Always approve", so it is **advisory** ([HITL](https://docs.nexos.ai/workspace/agents/human-in-the-loop-hitl-approvals.md)).
- **Custom MCP:** Streamable HTTP only, reachable from the internet. Auth is per-user API keys: "Each user will be able to provide their own API key individually." Org-wide once enabled; IP allowlisting on request. **No OAuth and no per-tool limits documented** ([Custom MCP](https://docs.nexos.ai/workspace/integrations/custom-mcp.md)).

![nexos integrations are Active or Inactive](images/nexos-integrations-status.png)

*Source: [nexos.ai docs, Integrations](https://docs.nexos.ai/workspace/integrations.md).*

![Custom MCP auth is per-user API keys](images/nexos-custom-mcp-auth.png)

*Source: [nexos.ai docs, Custom MCP](https://docs.nexos.ai/workspace/integrations/custom-mcp.md).*

**What works:** built-in Gmail Inactive, the guard as a custom MCP with personal keys ([nexos-setup.md](nexos-setup.md)), and nexos's Gmail app blocked at Google [GWA]. Untested: whether a key entered at setup becomes a default for members (everyone would then read that mailbox); test with a throwaway account.

## 6. Provider and infrastructure side

### 6.1 Gmail OAuth scopes

From [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes):

| Scope | Allows | Class |
|---|---|---|
| `gmail.readonly` | View messages and settings | Restricted |
| `gmail.send` | Send | Sensitive |
| `gmail.compose` | Manage drafts and send | Restricted |
| `gmail.modify` | Read, compose, send (no permanent delete) | Restricted |
| `https://mail.google.com/` | Everything, including permanent delete | Restricted |

A token with only `gmail.readonly` (plus `openid email`) can't send, draft, label or delete, **whatever the client does**. Restricted scopes need Google verification, except for an **Internal** app (GCP project owned by our Workspace org) ([verification exceptions](https://support.google.com/cloud/answer/13464323)).

### 6.2 Google Workspace admin controls [GWA]

Admin console > Security > Access and data control > **API controls** ([Google](https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data); needs the Service Settings admin privilege):

- **Per app, per OU:** Trusted, Limited, **Specific Google data** ("only to scopes that you specify", plus sign-in scopes), or **Blocked**.
- **Gmail as a Restricted service:** untrusted apps "stop working, and tokens are revoked." Large blast radius; coordinate first.
- **Unconfigured apps:** can be limited; leave "Trust internal apps" unchecked.
- **Accessed apps** lists apps holding tokens and their scopes, for audits.
- **Domain-wide delegation** ([162106](https://support.google.com/a/answer/162106)) can be limited to `gmail.readonly`, but can impersonate every user. Per-user OAuth is safer.

### 6.3 Google's own Gmail MCP server (Developer Preview)

From [Configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) and the [MCP reference](https://developers.google.com/workspace/gmail/api/reference/mcp):

- **Endpoint:** `https://gmailmcp.googleapis.com/mcp/v1`, Streamable HTTP, OAuth.
- **Requires:** enrolment in the Workspace Developer Preview Program (preview terms); the Gmail MCP API (`gmailmcp.googleapis.com`) enabled; an OAuth client marked "**Use this client for an AI-powered agent**"; and screening of prompts and responses for prompt injection, with Model Armor or your own solution ([Workspace MCP security](https://developers.google.com/workspace/guides/configure-mcp-security)).
- **Claude setup:** Web OAuth client with redirect `https://claude.ai/api/mcp/auth_callback`, added as a custom connector with your own client ID and secret.
- **Scopes:** Google's guide requests `gmail.readonly` **and** `gmail.compose`.
- **Tools:** `search_threads`, `get_thread`, `get_message`, `list_labels` and `list_drafts` work with `gmail.readonly`; draft and label tools need more. **There is no send tool.**
- Whether Claude's connector can request only `gmail.readonly` isn't documented. A Google admin can enforce it with Specific Google data [GWA]; whether sign-in then fails is **untested**.

### 6.4 MCP tool annotations: does anything enforce them?

No. The [MCP spec](https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md) says clients "MUST consider tool annotations to be untrusted unless they come from trusted servers". Claude uses them only for defaults and grouping ([review criteria](https://claude.com/docs/connectors/building/review-criteria.md)). A proxy should set them accurately but enforce on its own.

### 6.5 Payhawk MCP

From Payhawk's Help Center: [About](https://payhawk.com/help/payhawk-mcp-overview.md), [Connecting](https://payhawk.com/help/connecting-payhawk-mcp.md), [Tool reference](https://payhawk.com/help/payhawk-mcp-tool-reference.md), [FAQ](https://payhawk.com/help/faq-on-the-payhawk-mcp.md), [Developer API](https://payhawk.com/help/overview-of-the-payhawk-developer-api.md).

- **Official and hosted:** `https://mcp.payhawk.com/mcp`, Streamable HTTP.
- **Auth:** OAuth with the user's own Payhawk login, so Payhawk roles still apply.
- **Client allowlist:** "Payhawk accepts sign-ins only from the AI Assistants in the table" (Claude, Claude Code, ChatGPT, Cursor and others, **not nexos.ai**). Others fail with `host-not-allowed`; requests go to support@payhawk.com.
- **Write tools:** 82 tools, many write. Writes "always show a preview and need your confirmation" (advisory); it "never starts a payment or a transfer".
- **Developer API:** keys can be **Read-only**, but are account-wide, not per user.

![Payhawk accepts sign-ins only from the listed AI apps; nexos.ai is not listed](images/payhawk-supported-clients.png)

*Source: [Payhawk Help Center](https://payhawk.com/help/connecting-payhawk-mcp.md).*

The read / write split of all 82 tools is in [runbooks/payhawk.md](runbooks/payhawk.md#tool-classification-82-tools). It is **Inferred from tool names**, so block anything unclear.

## 7. The custom read-only proxy route

Built in this repo as [MCP Guard](../README.md) ([design](proxy-design.md)): a default-deny allowlist, `gmail.readonly` tokens only, the GA Gmail API by default (Google's Gmail MCP by config), OAuth sign-in for Claude and personal keys for nexos. Payhawk wiring and a real deployment are still open ([DEPLOY.md](../DEPLOY.md#open-items)).

## 8. Recommended layered setup

### 8.1 Target: real read-only guarantee (proxy route), Team or Enterprise

1. **Google [GWA]:** only our Internal OAuth client reaches Gmail, limited to `gmail.readonly`; "Claude for Gmail" and nexos's Gmail app Blocked.
2. **Guard:** allowlisted read tools, read-only tokens.
3. **Claude org:** built-in Gmail off (Team: decline and block requests); guard as a custom connector; Payhawk write tools Blocked. Enterprise: custom roles Block Gmail; verified-domain restriction on.
4. **Clients:** Claude Code managed MCP allowlist (server-managed **plus** MDM); Desktop: local MCP off, `forceLoginOrgUUID`.
5. **nexos:** Gmail Inactive; guard as custom MCP.
6. **Monitor:** guard audit log, Google OAuth logs, Accessed apps.

> **Team-plan gap:** without the verified-domain restriction, a member's personal Claude account can connect work Gmail. Only the Google admin blocking "Claude for Gmail" [GWA] closes it.

### 8.2 Lighter option: built-in connector, Anthropic-enforced read-only

Enable Gmail and set its write/delete group to **Blocked** (Claude Code included). Enterprise: verified-domain restriction on. [GWA] set "Claude for Gmail" to Trusted only for the OUs that use it. Fine if the team trusts Anthropic's policy layer.

### 8.3 Residual risks of 8.2 (why 8.1 is the guarantee)

- The token holds `gmail.compose`, which can send. A policy bug or a new unblocked write tool makes sending possible.
- Anthropic's docs disagree about what the connector can do, and its tools change without notice.
- On Team, personal Claude accounts can connect work Gmail unless Google blocks the app (which also blocks the org's use).
- Claude's policy doesn't cover nexos.

## 9. Payhawk-specific recommendation

- **Claude only:** add `https://mcp.payhawk.com/mcp` as an org custom connector and **Block every write and unclear tool**. Enforced server-side, no code, per-user Payhawk permissions kept.
- **nexos, or a credential-level guarantee:** put the guard in front of Payhawk ([design](proxy-design.md#payhawk)). That needs **Payhawk Support to allowlist the guard's redirect URI**; until then sign-in fails with `host-not-allowed`.
- Payhawk roles are another hard floor, per user rather than per tool.

Steps: [runbooks/payhawk.md](runbooks/payhawk.md).

## 10. Method and evidence

No accounts were connected and no consent was given. Pages were fetched with `curl`, GitHub issues with the GitHub CLI. Not captured: nexos.ai/pricing (Cloudflare bot check) and the built-in connector's live tool list (needs a real Google consent). Also used: Anthropic [release notes](https://support.claude.com/en/articles/12138966-release-notes), Claude Code [settings reference](https://code.claude.com/docs/en/settings-reference.md), nexos [changelog](https://docs.nexos.ai/changelog/changelog/latest-updates.md), MCP [authorization security](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations.md). Gateway sources: [proxy-design.md](proxy-design.md#reuse-an-existing-gateway-or-build).
