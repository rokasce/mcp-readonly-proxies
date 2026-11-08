# Adding the Gmail read-only proxy to nexos.ai

nexos.ai can only switch whole integrations on or off; it has no per-tool restriction.
To give nexos users read-only Gmail, keep nexos's built-in Gmail integration off and add
this proxy as a **custom MCP** server. nexos custom MCP speaks Streamable HTTP to an
internet-reachable URL and authenticates each user with their own API key, which is the
proxy's personal-key mode.

Prerequisite: the proxy is deployed and reachable at `https://gmail-ro.example.com`
([deploy.md](deploy.md)).

## Organization setup [nexos Owner]

1. **Turn off the built-in Gmail integration**: Settings > Organization > Integrations
   (or Management > Apps) > Gmail > **Inactive**. Otherwise members could still connect
   Gmail through nexos's own app, whose scopes are not documented.
2. **Add the custom MCP**: Management > Apps > **Add custom MCP**:
   - **Server name**: `Gmail (read-only)`
   - **Server URL**: `https://gmail-ro.example.com/mcp`
   - **Authentication**: API key, provided by each user individually.
   - **Configuration**: none needed.
3. **Test connection**: before any key is entered, the proxy answers `401` to
   unauthenticated MCP requests. If the test needs a key, create one for yourself first
   (below) and test with it.
4. Optional: ask nexos for their egress IPs and restrict the proxy's ingress to them plus
   your users' networks.

Custom MCP servers are organization-wide in nexos: once enabled, every team and member
can use it, but each member still needs their own key, and each key reads only that
member's own mailbox.

## Per-user setup [each user]

1. Open `https://gmail-ro.example.com/` and choose **Sign in with Google**. Only
   accounts in your Workspace domain are accepted, and Google asks only for read access
   to Gmail.
2. On the keys page, enter a label such as `nexos.ai` and choose **Create key**. Copy
   the key (it starts with `mrp_` and is shown once).
3. In nexos, open the "Gmail (read-only)" app and paste the key where nexos asks for your
   API key.
4. Enable the integration in a chat (Tools panel) or an agent (Agent Settings >
   Integrations).

The proxy accepts the key as `Authorization: Bearer <key>` or in an `X-API-Key` header.
nexos does not document which header it sends; if it uses another header name, add it to
`client_auth.api_key_headers` in the config.

## What users get

Five read-only tools: `search_threads`, `get_thread`, `get_message`, `list_labels` and
`list_drafts`. All are advertised with `readOnlyHint: true`, so nexos's
human-in-the-loop approvals should treat them as read-only actions. Nothing that sends,
drafts, labels, archives or deletes exists on this server, and the Google token behind
it cannot do any of that either.

## Revoking access

- A user revokes a key on the keys page (**Revoke**). nexos calls with that key fail
  with `401` immediately.
- **Disconnect and revoke everything** deletes the user's stored Google access, revokes
  it at Google and revokes all their keys.
- An admin can remove the user's access at Google (Admin console > the user > Security >
  Connected applications) or block the OAuth client for an OU [GWA].

## Not verified yet

nexos access was not available while building this, so these are untested against the
real product: the exact header nexos sends the key in, whether "Test connection" works
before a key exists, and how nexos HITL classifies the tools. Check them during the
pilot.
