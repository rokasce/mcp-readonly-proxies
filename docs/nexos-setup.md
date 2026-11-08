# Adding the guard to nexos.ai

nexos.ai can only turn whole integrations on or off, not single tools. So read-only Gmail in nexos
means: built-in Gmail **off**, and the guard added as a **custom MCP**, with each user's own
personal key. Background: [findings.md](findings.md).

**Before you start:** the guard is deployed with `MCP_GUARD_PERSONAL_KEYS=true` at
`https://gmail-guard.example.com`, and `https://gmail-guard.example.com/keys/callback` is in the
Google OAuth client's redirect URIs ([DEPLOY.md](../DEPLOY.md)). The same deployment keeps serving
Claude.

## Organization setup [nexos Owner]

1. **Turn off built-in Gmail:** Settings > Organization > Integrations (or Management > Apps) >
   Gmail > **Inactive**. Its tools and scopes are undocumented.
2. **Block nexos's Gmail app at Google [GWA]:** a Workspace admin sets it to **Blocked** (or
   Specific Google data) under Accessed apps, so nobody gets write access through it
   ([google-workspace-admin.md](runbooks/google-workspace-admin.md)).
3. **Add the custom MCP:** Management > Apps > **Add custom MCP**:
   - Server name: `Gmail (read-only)`
   - Server URL: `https://gmail-guard.example.com/mcp`
   - Authentication: API key, provided by each user individually
   - Configuration: none
4. **Test connection: don't paste your own key.** Without a key the guard answers `401`. If the
   test needs a key, nexos doesn't say whether a key entered here becomes the default for members
   who haven't added theirs; if it does, every member would read your mailbox. Use a key from a
   throwaway account in the domain, revoke it afterwards, and confirm that members without a key
   get no access.
5. Optional: ask nexos for its egress IPs and limit who can reach the guard (keep your users and
   Claude.ai, if used).

A custom MCP is organization-wide: every team and member can use it once enabled. Each member
still needs their own key, and a key reads only its owner's mailbox.

## Per-user setup [each user]

1. Open `https://gmail-guard.example.com/keys` and choose **Sign in and create a key**. Only
   accounts in your Workspace domain work, and Google asks only for read access to Gmail.
2. Copy the key. It starts with `mcpg_`, is shown once, and is valid for 180 days.
3. In nexos, open the "Gmail (read-only)" app and paste the key as your API key.
4. Turn it on in a chat (Tools panel) or an agent (Agent Settings > Integrations).

One key per person: making a new key replaces the old one, which is also the fix for a lost key.
We don't know which header nexos sends the key in, so the guard accepts both
`Authorization: Bearer <key>` and `X-API-Key: <key>`.

## What users get

Six read-only tools: `search_threads`, `get_thread`, `get_message`, `list_labels`, `list_drafts`
and `get_draft`. Nothing can send, draft, label, archive or delete, and the Google token behind
them can't either.

## Revoking access

- **One key:** the user chooses **Sign in and revoke my key** on the keys page. The key fails with
  `401` at once and the guard deletes the Google access it kept for it.
- **Everything, Claude included:** remove the app at Google (the person under their Google
  Account's third-party apps, or an admin under the user's Security > Connected applications), or
  block the OAuth client for an OU [GWA].

## What nexos can't do

- **No per-tool or per-team control.** Integrations are Active or Inactive for the whole
  organization; Team Owners don't manage apps.
- **Approvals (HITL) are advisory.** An agent pauses before actions that change something, but any
  user can pick "Always approve", and read-only actions run without asking. nexos doesn't document
  how it tells read from write. The guard's tools carry `readOnlyHint: true`, so they should count
  as read-only. Don't rely on HITL as the read-only control.
- **No OAuth for custom MCP** is documented, hence personal keys.
- **Claude Code through the nexos gateway** (`ANTHROPIC_BASE_URL=https://api.nexos.ai`) gets no
  claude.ai connectors or server-managed settings; only MDM or file-deployed managed settings
  govern it ([claude-code-managed-settings.md](runbooks/claude-code-managed-settings.md)).
- **Payhawk** doesn't allow sign-in from nexos today ([payhawk.md](runbooks/payhawk.md)).

Worth asking nexos: OAuth for custom MCP (no keys needed then), its egress IPs, and whether any
control depends on the plan.

## Not verified yet

We had no nexos access while building this, so these are untested against the real product:

- which header nexos sends the key in
- whether "Test connection" works before a key exists
- whether a key entered at setup is shared with members
- how nexos HITL classifies the guard's tools

Check them during the pilot. Open questions are tracked in [questions.md](questions.md).
