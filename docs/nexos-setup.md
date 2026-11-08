# Adding the guard to nexos.ai

nexos.ai can only switch whole integrations on or off; it has no per-tool restriction. To give
nexos users read-only Gmail, keep nexos's built-in Gmail integration off and add the guard as a
**custom MCP** server. nexos custom MCP speaks Streamable HTTP to an internet-reachable URL and
authenticates each user with their own API key, which is the guard's personal key.

Prerequisite: the guard is deployed with `MCP_GUARD_PERSONAL_KEYS=true` and reachable at
`https://gmail-guard.example.com`, with `https://gmail-guard.example.com/keys/callback` added to
the Google OAuth client's redirect URIs ([DEPLOY.md](../DEPLOY.md)). The same deployment keeps
serving Claude over OAuth.

## Organization setup [nexos Owner]

1. **Turn off the built-in Gmail integration**: Settings > Organization > Integrations (or
   Management > Apps) > Gmail > **Inactive**. Otherwise members could still connect Gmail through
   nexos's own app, whose scopes are not documented.
2. **Add the custom MCP**: Management > Apps > **Add custom MCP**:
   - **Server name**: `Gmail (read-only)`
   - **Server URL**: `https://gmail-guard.example.com/mcp`
   - **Authentication**: API key, provided by each user individually.
   - **Configuration**: none needed.
3. **Test connection**: the guard answers `401` to MCP requests without a key. If the test needs
   a key, make one for yourself first (below) and test with it.
4. Optional: ask nexos for their egress IPs and restrict the guard's ingress to them plus your
   users' networks (Claude.ai's servers need to reach it too, if Claude.ai connectors are used).

Custom MCP servers are organization-wide in nexos: once enabled, every team and member can use
it, but each member still needs their own key, and each key reads only that member's mailbox.

## Per-user setup [each user]

1. Open `https://gmail-guard.example.com/keys` and choose **Sign in and create a key**. Only
   accounts of your Workspace domain are accepted, and Google asks only for read access to Gmail.
2. Copy the key (it starts with `mcpg_` and is shown once). It is valid for 180 days.
3. In nexos, open the "Gmail (read-only)" app and paste the key where nexos asks for your API key.
4. Enable the integration in a chat (Tools panel) or an agent (Agent Settings > Integrations).

Each person has one key: making a new one replaces the old one, so a lost key is fixed by making
a new one. The guard accepts the key as `Authorization: Bearer <key>` or in an `X-API-Key`
header.

## What users get

Six read-only tools: `search_threads`, `get_thread`, `get_message`, `list_labels`, `list_drafts`
and `get_draft`, all advertised with `readOnlyHint: true`, so nexos's human-in-the-loop approvals
should treat them as read-only actions. Nothing that sends, drafts, labels, archives or deletes
exists on this server, and the Google token behind it can't do any of that either.

## Revoking access

- A user chooses **Sign in and revoke my key** on the keys page. Calls with that key fail with
  `401` at once, and the guard deletes the Google access it kept for the key.
- To cut a person off entirely (Claude too): remove the app at Google (the person under their
  Google Account's third-party apps, or an admin under the user's Security > Connected
  applications), or block the OAuth client for an OU [GWA].

## Not verified yet

nexos access was not available while building this, so these are untested against the real
product: the header nexos sends the key in, whether "Test connection" works before a key exists,
and how nexos HITL classifies the tools. Check them during the pilot.
