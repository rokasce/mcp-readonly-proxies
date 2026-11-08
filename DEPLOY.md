# Deploying MCP Guard

How to host the guard so people add it once and sign in with Google, and how Claude and
nexos.ai connect. Do the steps in order; settings are explained in the [README](README.md#settings).
For `--dev`, see [local test mode](README.md#local-test-mode-no-oauth-client).

**Status (2026-11-08):** tried in `--dev` and against a fake Google; not yet deployed or tried with
real Google sign-in. **Open** marks what still needs a decision or a check.

## 1. Google OAuth client (needed for any deployment)

Google only gives tokens to a registered OAuth client, so the deployed guard won't start without
one, gateway or not. Someone with Google Cloud access in the example.com organisation creates:

1. **A Google Cloud project owned by the organisation** (or an existing internal one); otherwise
   [Internal isn't offered](https://support.google.com/cloud/answer/15549945#user-type-internal).
2. **OAuth consent screen, user type Internal** (Google Auth Platform > Audience). Only example.com
   accounts can sign in, and the [restricted](https://developers.google.com/workspace/gmail/api/auth/scopes#restricted-scopes) `gmail.readonly` scope
   [needs no Google verification](https://support.google.com/cloud/answer/13464323#:~:text=Internal%20Use%20apps,designated%20as%20internal%2Donly).
3. **OAuth client ID, type Web application**, with redirect URIs `https://<guard>/auth/callback`
   and, if personal keys are on, `https://<guard>/keys/callback`. No JavaScript origins. Decide
   on [**Use this client for an AI-powered agent**](https://support.google.com/cloud/answer/15549257#agentic-client) when creating it
   ([questions.md](docs/questions.md), question 13).
4. **Gmail API enabled** (`gmail.googleapis.com`). Gmail MCP also needs `gmailmcp.googleapis.com`
   enabled and the client marked **Use this client for an AI-powered agent**
   ([README](README.md#gmail-api-or-gmail-mcp)).

The secret goes straight into the secret store, never into chat, tickets or the repo.

Then a **Google Workspace admin** ([runbook](docs/runbooks/google-workspace-admin.md)):

5. Admin console > Security > Access and data control > **API controls** > Manage Third-Party App
   Access > [Configure new app](https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data#configure-a-new-app) > search by **client ID** > **Specific Google data** with only
   `gmail.readonly` plus the sign-in scopes (or **Trusted**), for the OUs that use the guard.
6. Set Anthropic's "Claude for Gmail" and nexos.ai's Gmail app to [**Blocked**](https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data#trustorlimit), if Gmail should be
   reachable only read-only. A Blocked app can't reach any Google service, so people lose those
   apps' Gmail features at once; coordinate first.

### Scopes

Add these on the consent screen (Google Auth Platform > Data access); nothing else.

| Scope | Type | Why |
|---|---|---|
| `openid` | Non-sensitive | Signs the person in |
| `https://www.googleapis.com/auth/userinfo.email` | Non-sensitive | Verified email, for the domain check and logs |
| `https://www.googleapis.com/auth/gmail.readonly` | [Restricted](https://developers.google.com/workspace/gmail/api/auth/scopes#restricted-scopes) | Reads mail, drafts and labels; Google refuses any change |

The first two are fixed in code (`IDENTITY_SCOPES` in `config.py`), the Gmail one comes from
`google_scopes` in `guard.yaml` (`.readonly` only); no environment variable sets scopes. A token
holding any other scope is refused and revoked. Unlike in `--dev`, `openid` is required.

## 2. Settings for the deployment

| Variable | Value |
|---|---|
| `MCP_GUARD_UPSTREAM` | `gmail` |
| `MCP_GUARD_BASE_URL` | The guard's public https URL, as in the redirect URIs |
| `MCP_GUARD_GOOGLE_CLIENT_ID`, `MCP_GUARD_GOOGLE_CLIENT_SECRET` | From step 1; secret masked |
| `MCP_GUARD_ALLOWED_DOMAIN` | `example.com` |
| `MCP_GUARD_JWT_SIGNING_KEY` (recommended) | Long random value, masked |
| `MCP_GUARD_STORAGE_URL` | `rediss://...` (see [Hosting](#3-hosting)) |
| `MCP_GUARD_STORAGE_ENCRYPTION_KEYS` (recommended) | A Fernet key, masked: `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` |
| `MCP_GUARD_PERSONAL_KEYS` | `true` for nexos.ai |
| `MCP_GUARD_CLIENT_REDIRECT_URIS` | Unset for Claude only; see [Other MCP clients](#other-mcp-clients) |

Secrets go where `internal-mcp` keeps its own for now: masked, protected CI/CD variables passed to
the pod as environment variables.

## 3. Hosting

- **Image:** the root `Dockerfile`; a failing test fails the build. Runs as non-root (uid 10001)
  on port 3000.
- **Storage:** client registrations, Google tokens and personal-key hashes live in one key-value
  store, encrypted with `MCP_GUARD_STORAGE_ENCRYPTION_KEYS`.
  - **More than one replica: Redis** (`rediss://...`). The guard keeps no session state, so with
    shared storage any replica serves any request.
  - **One replica:** a persistent volume at `FASTMCP_HOME` (`/app/data` in the image) or
    `MCP_GUARD_STORAGE_URL=file:///data`. Without one, everyone signs in again after each restart.
    The rest of the filesystem can be read-only.
  - **Key rotation:** put the new key first, keep the old one until every value is rewritten or
    expired, then drop it. Values only the dropped key could read just mean signing in again.
- **Health check:** `GET /health` for liveness and readiness (no sign-in, no Gmail call).
- **Network:** inbound https from browsers, Claude.ai and nexos.ai (so internet-reachable).
  Outbound to `gmail.googleapis.com` (or `gmailmcp.googleapis.com`), `oauth2.googleapis.com`,
  `accounts.google.com`, `www.googleapis.com` and Redis. Upstream calls time out after 60 s.
- **TLS:** terminate in front of the guard. `MCP_GUARD_BASE_URL` must be https; Google [refuses
  non-https redirect URIs except localhost](https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation).
- **Logs:** one JSON audit line per tool call on stdout ([format](docs/configuration.md#audit-log)),
  plus warnings for refused people, refused tokens and changed pinned tools. Alert on
  `"decision": "deny"` with `"outcome": "refused"` (an agent trying to write) and on pin drift.
- **Limits:** per person 2 requests per second, bursts of 20; results cut at 1 MB (`guard.yaml`),
  so one runaway agent can't use up the shared Gmail quota. **The rate limit is per replica:** with
  N replicas a person can make up to N times as many requests.

### Behind the MCP gateway

`internal-mcp` sits behind the company MCP gateway (provisional), which signs people in with their
AD identity. That isn't a Google token, so the guard still runs its own Google sign-in. **Open:**
can the gateway pass the MCP OAuth flow through to the guard unchanged, or is the guard exposed
directly with its own URL? It is built for direct exposure today.

## 4. Connecting clients

The guard serves MCP over Streamable HTTP at `https://<guard>/mcp` with standard MCP OAuth
([client metadata documents, CIMD](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration#client-id-metadata-documents), as Claude uses, or [dynamic client registration](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration#dynamic-client-registration)). **Only
callbacks in `MCP_GUARD_CLIENT_REDIRECT_URIS` are accepted** (Claude only by default), so another
app can't collect people's Gmail access.

### Claude Code

`claude mcp add --transport http gmail https://<guard>/mcp`, then sign in when asked (or from
`/mcp`; [Claude Code docs](https://code.claude.com/docs/en/mcp#authenticate-with-remote-mcp-servers)). Its [`http://localhost:<port>/callback`](https://claude.com/docs/connectors/building/authentication#callback-urls) is allowed by default. To roll it out to everyone,
use managed settings ([runbook](docs/runbooks/claude-code-managed-settings.md)).

### Claude.ai and Claude Desktop

1. Add a custom connector with URL `https://<guard>/mcp` and, under OAuth client, choose [**Use
   Claude's published identity**](https://claude.com/docs/connectors/custom/add-unlisted#choose-authentication-settings).
2. Connect and sign in with Google.
3. Organisation Owners can add it once for everyone, set its tools to [Always allow](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities#h_9fa8289074) (all are
   reads), and block the built-in Gmail connector ([runbook](docs/runbooks/claude-org.md)).

Claude.ai calls the guard from [Anthropic's outbound ranges `160.79.104.0/21` and `153.61.208.0/20`](https://platform.claude.com/docs/en/api/ip-addresses#:~:text=Outbound%20IP%20addresses) and gives its sign-in
endpoints [10 seconds](https://claude.com/docs/connectors/building/authentication#endpoint-latency) to answer. Browsers must also reach `/authorize`, the Google callback and
`/keys`, so don't limit the guard to Anthropic's and nexos's addresses.

### nexos.ai

nexos takes a [per-person API key](https://docs.nexos.ai/workspace/integrations/custom-mcp#add-the-custom-mcp-to-nexos.ai), so set `MCP_GUARD_PERSONAL_KEYS=true`. Each person makes a key
at `https://<guard>/keys` and pastes it into nexos ([docs/nexos-setup.md](docs/nexos-setup.md)).

### Other MCP clients

Add the client's callback to `MCP_GUARD_CLIENT_REDIRECT_URIS`; until then `/register` refuses it
(HTTP 400). The variable replaces the default, so repeat Claude's entries:

```
MCP_GUARD_CLIENT_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback,http://localhost:*/callback,http://127.0.0.1:*/callback,<other client's callback>
```

Only allow a fixed callback URL the vendor owns; a wildcard host would let any site register.
Clients that only take a fixed API key can use personal keys.

## 5. After deploying: check it works

```bash
curl https://<guard>/health                       # 200 {"status":"ok","upstream":"gmail"}
curl -i -X POST https://<guard>/mcp               # 401 with a WWW-Authenticate header
curl https://<guard>/.well-known/oauth-protected-resource/mcp   # scopes: openid, userinfo.email, gmail.readonly
```

1. In Claude Code, sign in and call `list_labels`: the audit line shows your email and
   `"outcome": "ok"`.
2. Ask the agent to send a mail: it has no such tool; a direct call is audited `"decision": "deny"`.
3. An account outside example.com (if it gets past the consent screen) is told "Only example.com
   accounts can use this server."
4. Make a personal key on `/keys`, use it, revoke it: the next call gets 401.

## Open items

- Google OAuth client and project (step 1), and the Workspace admin's API controls; what Gmail
  MCP needs if it's ever used.
- First end-to-end test with real Google sign-in, from Claude.ai, Claude Code and nexos.ai.
- Redis (or a volume), and the storage encryption key in the secret store.
- A GitLab CI pipeline using the shared `build-image` component, like `internal-mcp`; only GitHub
  Actions exists today.
- Exposed directly or [behind the MCP gateway](#behind-the-mcp-gateway).
- nexos.ai: which header carries the key (`Authorization: Bearer` and `X-API-Key` both work),
  whether "Test connection" works before a key exists, and whether a key entered at setup is
  shared with members ([docs/nexos-setup.md](docs/nexos-setup.md)).

## Things to keep in mind

- **The guard limits only itself.** Other apps with a Gmail grant (Anthropic's Gmail connector,
  nexos's Gmail app) still reach Gmail. Block them (step 6 and the Claude runbook).
- **Read-only isn't harmless.** A mail can carry instructions an agent follows (prompt injection),
  and an agent that can also write or fetch elsewhere can be steered into copying mail out. Keep
  agents with Gmail away from tools that send data out, or require approval for those.
- **Google keeps at most 100 refresh tokens per person per OAuth client**, silently dropping the
  oldest ([OAuth 2.0](https://developers.google.com/identity/protocols/oauth2#expiration)). Each sign-in and personal key uses one, so very frequent sign-ins can break an old one.
- **Personal keys are bearer credentials** to a whole mailbox for 180 days, stored by nexos. Revoke
  a key when someone leaves nexos. Suspending the person in Workspace also stops it.
- **Google's tokeninfo endpoint**, where the guard checks tokens (cached up to a minute), "may be
  throttled" ([OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect#validatinganidtoken)).
  At large scale or if throttling shows in the logs, verify tokens another way.
- **One domain.** `MCP_GUARD_ALLOWED_DOMAIN` takes one Workspace domain, compared with the
  account's [`hd`](https://developers.google.com/identity/openid-connect/openid-connect#an-id-tokens-payload). In a multi-domain Workspace, check what `hd` a secondary-domain account gets.
- **A Claude connector's sign-in settings are fixed once added** (its name and URL can still be
  edited). Changing the OAuth client choice means re-adding it, and everyone connects again ([Claude docs](https://claude.com/docs/connectors/custom/add-unlisted#edit-or-remove-a-connector)).
