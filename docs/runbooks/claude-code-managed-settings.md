# Runbook: Claude Code managed settings

For IT and Claude Owners: lock Claude Code to approved MCP servers and deny Gmail write tools, on top of the claude.ai tool policy. Background: [findings.md](../findings.md#42-claude-code). Facts are from the linked Claude Code docs, checked on 2026-11-08.

## Steps

1. Write `managed-settings.json` from the [example](#example-proxy-route) below, with our real URLs.
2. Push it with MDM, or copy it to:
   - macOS: `/Library/Application Support/ClaudeCode/managed-settings.json`
   - Linux and WSL: `/etc/claude-code/managed-settings.json`
   - Windows: `C:\Program Files\ClaudeCode\managed-settings.json`
3. Owners: set the same settings as server-managed settings in claude.ai.
4. [Verify](#verify) on one machine.

## Which sessions each delivery method reaches

| Delivery | Set by | Reaches |
|---|---|---|
| **Server-managed** | Owners, in claude.ai | Only sessions signed in with a Team or Enterprise claude.ai login. Refreshed hourly |
| **Endpoint-managed** (MDM, registry, `managed-settings.json`) | IT | Every session; "stronger security guarantees" ([server-managed-settings.md](https://code.claude.com/docs/en/server-managed-settings.md)) |

Sessions using an API key, auth token, `apiKeyHelper`, a third-party provider or the [nexos.ai gateway](https://docs.nexos.ai/gateway-api/integrations/claude-code.md) get no server-managed settings and no claude.ai connectors ([mcp.md](https://code.claude.com/docs/en/mcp.md#use-mcp-servers-from-claude-ai)). **Use both**: without MDM, a member escapes the controls by switching to an API key.

## Controls

| Setting | Effect |
|---|---|
| Org tool policy ([claude.ai](claude-org.md)) | `blocked` tools are removed; `ask` prompts every call. Desktop local and SSH sessions only get `blocked` |
| `permissions.deny` | Any level's deny wins; managed rules can't be overridden. `allowManagedPermissionRulesOnly` ignores non-managed rules. `mcp__` rules with parentheses are skipped ([permissions.md](https://code.claude.com/docs/en/permissions.md)) |
| `allowManagedMcpServersOnly` + `allowedMcpServers`, `deniedMcpServers` | Only allowlisted servers load; the denylist always wins. `serverName` is "not a security control"; use `serverUrl` ([managed-mcp.md](https://code.claude.com/docs/en/managed-mcp.md)) |
| `managed-mcp.json` | Loads only listed servers, hides claude.ai connectors unless `allowAllClaudeAiMcps: true`. MDM only |
| `managedMcpServers` | Gives a server to every user |
| `oauth.scopes` | Pins scopes for a user-added OAuth server |
| `PreToolUse` hook + `allowManagedHooksOnly` | Can deny any call; can't override a deny rule |
| `disableClaudeAiConnectors: true` | Turns off claude.ai connectors |

![How Claude Code applies the org's blocked and ask settings](../images/claude-code-org-connector-controls.png)

*Source: [Claude Code docs, MCP](https://code.claude.com/docs/en/mcp.md#organization-controls-on-connector-tools), captured 2026-11-08.*

## Example: proxy route

Only the read-only proxies load; the built-in Gmail connector, Google's Gmail MCP and the direct Payhawk MCP are denied; Gmail write tools are denied on every server.

```json
{
  "allowManagedMcpServersOnly": true,
  "allowedMcpServers": [
    { "serverUrl": "https://gmail-guard.example.com/*" },
    { "serverUrl": "https://payhawk-ro.example.com/*" }
  ],
  "deniedMcpServers": [
    { "serverName": "claude.ai Gmail" },
    { "serverUrl": "https://gmailmcp.googleapis.com/*" },
    { "serverUrl": "https://mcp.payhawk.com/*" }
  ],
  "allowManagedPermissionRulesOnly": true,
  "permissions": {
    "deny": [
      "mcp__*__send_message", "mcp__*__reply", "mcp__*__forward",
      "mcp__*__create_draft", "mcp__*__update_draft",
      "mcp__*__trash_message", "mcp__*__trash_thread"
    ]
  }
}
```

- **Built-in route:** keep only the `allowManagedPermissionRulesOnly` and `permissions` part as a second layer behind the org Blocked policy. claude.ai tools are named `mcp__claude_ai_Gmail__<tool>` ([findings 3.1](../findings.md#31-what-the-connector-exposes)).
- Keep `allowManagedMcpServersOnly` in the managed file, or allowlists merge from every scope, including the user's.
- The guard's `serverUrl` entry also admits its claude.ai custom connector, since fetched connectors are checked by URL.
- `deniedMcpServers` accepts a claude.ai display name, but names can change. `allowedMcpServers` needs `serverUrl`.
- Use `mcp__*__` globs. Desktop's Code tab mounts connectors under UUIDs, so `mcp__claude_ai_Gmail__*` doesn't match there (**Community**, [#92787](https://github.com/anthropics/claude-code/issues/92787)). The org Blocked policy is the reliable control for Desktop.

## Example: provide the proxy to every user

```json
{
  "managedMcpServers": {
    "gmail-guard": { "type": "http", "url": "https://gmail-guard.example.com/mcp" }
  }
}
```

Needs Claude Code v2.1.259+ and an `https://` URL; no `command`, `args`, `env`, `headersHelper` or `${VAR}`. Claude Desktop's setting of the same name has a different shape. For a user-added server, pin scopes in `.mcp.json` with `"oauth": { "scopes": "https://www.googleapis.com/auth/gmail.readonly" }`.

## Example: managed PreToolUse hook

Optional backstop for write-shaped names the deny list misses ([hooks.md](https://code.claude.com/docs/en/hooks.md)).

```json
{
  "allowManagedHooksOnly": true,
  "hooks": {
    "PreToolUse": [{
      "matcher": "mcp__.*__(send|reply|forward|create_draft|update_draft|trash|mark_.*_spam|label|unlabel|create_label|update_label|delete_label).*",
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"MCP write tools are blocked by organization policy\"}}'"
      }]
    }]
  }
}
```

## Verify

- `/mcp` and `claude mcp list`: a blocked server doesn't appear.
- `claude doctor`: shows resolved settings and skipped rules.
- `claude mcp add` with a blocked URL fails with `not allowed by enterprise policy` or `explicitly blocked by enterprise policy`.
