# Deploying MCP Guard

How to take the guard from local test mode (`--dev`) to a hosted service that people add once
and sign in to with Google, and how each client (Claude Code, Claude.ai / Desktop, nexos.ai,
others) connects to it. Settings are listed in the [README](README.md#settings); this file is the
order to do things in and why.

Status (2026-11-08): runs locally, in `--dev` and with the full sign-in against a fake Google
(Claude Code's OAuth flow and a personal key tried end to end); not yet deployed or tried against
real Google sign-in. Items marked **Open** still need a decision or a check.

## Local test mode vs. deployed

| | `--dev` (local test mode) | Deployed |
|---|---|---|
| Who signs in | Nobody: you paste a Google token into the client's `Authorization` header | Each person signs in with Google through the guard (or makes a personal key) |
| Token | From OAuth Playground; expires after **1 hour**, then re-add the connector | The guard keeps each person's Google token, encrypted, and refreshes it |
| Listens on | 127.0.0.1 only | The guard's public https URL |
| Needs a Google OAuth client | No | **Yes** |

`--dev` is for trying the guard on your own machine; it can't be shared. To try the deployed
sign-in without a Google OAuth client, use the fake Google
([docs/local-development.md](docs/local-development.md)).

### Refreshing the token in `--dev`

When calls fail with 401 ("bearer token is invalid, expired..."), the token has expired:

1. In [OAuth Playground](https://developers.google.com/oauthplayground), authorize the scopes
   `https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email`
   (`openid` may be added but isn't needed; any other scope gets the token refused) and exchange
   the code for tokens; copy the access token.
2. Replace the connector (keeping its name keeps the tool names the same):
   ```bash
   claude mcp remove gmail
   claude mcp add --transport http gmail http://localhost:3000/mcp --header "Authorization: Bearer <token>"
   ```

Restarting the guard itself doesn't require re-adding the connector.

## 1. Google OAuth client (needed for any deployment)

The deployed guard runs the Google sign-in for each person and calls Gmail with that person's
own token. Google only hands out tokens to a registered OAuth client, so without one the guard
refuses to start outside `--dev`. Putting a gateway in front doesn't change this (see
[Behind the MCP gateway](#behind-the-mcp-gateway)).

Someone with Google Cloud access in the example.com organisation creates:

1. **A Google Cloud project** owned by the organisation (or reuse an existing internal one). It
   must belong to the Workspace organisation, or the Internal audience below isn't offered.
2. **OAuth consent screen, user type Internal** (Google Auth Platform > Audience). Only example.com
   Workspace accounts can then sign in at all, which Google enforces under the guard's own domain
   check. `gmail.readonly` is a restricted scope; an Internal app doesn't go through Google's
   verification for it.
3. **OAuth client ID, type Web application**, with the authorized redirect URIs
   `https://<guard public URL>/auth/callback` (Claude's sign-in) and, if personal keys are on,
   `https://<guard public URL>/keys/callback`. No JavaScript origins are needed.
4. **Gmail API enabled** in the project (`gmail.googleapis.com`), for the default `gmail_api`
   upstream. **Open:** whether Gmail MCP (`gmail-mcp`) needs anything beyond its preview enrolment.

The client ID and secret become `MCP_GUARD_GOOGLE_CLIENT_ID` and
`MCP_GUARD_GOOGLE_CLIENT_SECRET`. The secret goes straight into the secret store, never into
chat, tickets or the repo.

Then, at the Workspace level [Google Workspace admin]
([runbook](docs/runbooks/google-workspace-admin.md)):

5. Admin console > Security > Access and data control > **API controls** > Manage Third-Party App
   Access > Configure new app > search by the **client ID** > **Specific Google data** with only
   `gmail.readonly` plus the sign-in scopes (or **Trusted**), for the OUs that should use the guard.
6. Set Anthropic's "Claude for Gmail" and nexos.ai's Gmail app to **Blocked**, if Gmail should be
   reachable only read-only. Coordinate first: blocking revokes existing tokens for those apps.

### Scopes

Add these to the consent screen's scopes (Google Auth Platform > Data access). They are all the
guard asks for at sign-in; nothing else is needed.

| Scope | Type | Why |
|---|---|---|
| `openid` | Non-sensitive | Signs the person in |
| `https://www.googleapis.com/auth/userinfo.email` | Non-sensitive | The person's email and whether Google verified it, for the domain check and the logs |
| `https://www.googleapis.com/auth/gmail.readonly` | Restricted | Reads mail, threads, drafts and labels for the allowed tools; Google refuses any change |

The first two are fixed in the code (`IDENTITY_SCOPES` in `config.py`); the Gmail one comes from
`google_scopes` in `guard.yaml`, which only accepts `.readonly` scopes. The guard checks every
token Google hands it: a token holding any other scope is refused, and revoked at Google, so a
broader grant (say from an earlier app using the same client, or a consent screen with extra
scopes) can't be used through it.

The consent screen is the only place you enter them by hand: the guard already requests them at
sign-in, and no environment variable sets scopes. These are not the OAuth Playground scopes from
`--dev`. Playground isn't used in a deployment, and `openid` is required here but optional there.

## 2. Settings for the deployment

| Variable | Value |
|---|---|
| `MCP_GUARD_UPSTREAM` | `gmail` |
| `MCP_GUARD_BASE_URL` | The guard's public https URL, the same one used in the redirect URIs |
| `MCP_GUARD_GOOGLE_CLIENT_ID`, `MCP_GUARD_GOOGLE_CLIENT_SECRET` | From step 1; the secret is masked |
| `MCP_GUARD_ALLOWED_DOMAIN` | `example.com` |
| `MCP_GUARD_JWT_SIGNING_KEY` (recommended) | A long random value, masked. Left out, it's derived from the client secret, and rotating that secret signs everyone out |
| `MCP_GUARD_STORAGE_URL` | `rediss://...` (see step 3) |
| `MCP_GUARD_STORAGE_ENCRYPTION_KEYS` (recommended) | A Fernet key, masked: `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` |
| `MCP_GUARD_PERSONAL_KEYS` | `true` if nexos.ai (or another API-key client) will use the guard |
| `MCP_GUARD_CLIENT_REDIRECT_URIS` | Leave unset for Claude only; add other clients' callbacks here (see Other MCP clients below) |

Secrets go where `internal-mcp` keeps its own for now: masked, protected CI/CD variables that the
platform passes to the pod as environment variables. Any other `MCP_GUARD_*` variable makes the
guard refuse to start, so a typo can't silently switch something off.

## 3. Hosting

- **Image:** `Dockerfile` in the repo root: a build stage, a test stage whose marker the runtime
  stage needs (a failing test fails the build), and a non-root runtime (uid 10001). The command is
  `mcp-guard --port 3000`; it listens on `0.0.0.0` by default. GitHub Actions builds it on every
  pull request; see Open items for the company's GitLab pipeline.
- **Sign-in state:** client registrations, each person's encrypted Google tokens and personal-key
  hashes are kept in one key-value store, encrypted with `MCP_GUARD_STORAGE_ENCRYPTION_KEYS`.
  - **More than one replica: Redis** (`MCP_GUARD_STORAGE_URL=rediss://...`). The guard keeps no
    MCP session state of its own (stateless HTTP), so any replica can serve any request once the
    sign-ins are shared; a test signs in on one replica and calls through another.
  - One replica: a persistent volume and `MCP_GUARD_STORAGE_URL=file:///data` (or `FASTMCP_HOME`).
    The image sets `FASTMCP_HOME=/app/data`; mount the volume there. Without one, sign-ins vanish
    on every restart and everyone signs in again.
  - The root filesystem can be read-only as long as the storage path is the only writable one.
  - Rotating the encryption key: put the new key first, keep the old one after it until every
    value has been rewritten or has expired, then drop it. A value only the dropped key could read
    counts as missing (that person signs in again), not as an error.
- **Health check:** `GET /health` returns `{"status":"ok","upstream":"gmail"}`. It needs no
  sign-in and doesn't call Gmail, so use it for liveness and readiness.
- **Network:** inbound https from people's browsers and their MCP clients (Claude.ai's servers and
  nexos.ai call it too, so it must be reachable from the internet if they are used); outbound to
  `gmail.googleapis.com` (or `gmailmcp.googleapis.com`), `oauth2.googleapis.com`,
  `accounts.google.com` and `www.googleapis.com`, plus Redis.
- **TLS:** terminate in front of the guard. `MCP_GUARD_BASE_URL` must be https; Google refuses
  non-https redirect URIs except localhost.
- **Logs:** one JSON line per tool call on stdout (`mcp_guard.audit`, format in
  [docs/configuration.md](docs/configuration.md#audit-log)), plus a warning per refused person,
  refused token and pinned tool whose definition changed. Arguments (search queries, ids) and mail
  are never logged. Alert on `"decision": "deny"` with `"outcome": "refused"` for tools that aren't
  allowed (an agent trying to write), and on the pin-drift warning.
- **Limits:** per person 2 requests per second with bursts of 20, results cut at 1 MB (both in
  `guard.yaml`). Gmail API quota is shared by everyone on the OAuth client, so one runaway agent
  can't use it up.

### Behind the MCP gateway

`internal-mcp` sits behind the company MCP gateway (provisional), which signs people in with their
AD identity. That identity isn't a Google token, and Gmail needs one, so the guard still runs its
own Google sign-in. **Open:** whether the gateway can pass the MCP OAuth flow through to the guard
unchanged, or whether the guard is exposed directly with its own URL. Exposed directly is what it's
built for today.

## 4. Connecting clients

The guard serves MCP over Streamable HTTP at `https://<guard>/mcp`. A client signs people in with
standard MCP OAuth: it registers itself with the guard (dynamic client registration, or a client
metadata document as Claude Code uses), sends the person to sign in, and is called back on its own
redirect URI. **Only callbacks in `MCP_GUARD_CLIENT_REDIRECT_URIS` may register**, so another app
can't collect people's Gmail access; by default that is Claude only.

### Claude Code

```bash
claude mcp add --transport http gmail https://<guard>/mcp
```
Then sign in when asked (or from `/mcp`). Its callback (`http://localhost:<port>/callback`) is
allowed by default. To push it to everyone, use managed settings
([runbook](docs/runbooks/claude-code-managed-settings.md)).

### Claude.ai and Claude Desktop

Add a custom connector with the URL `https://<guard>/mcp`, then connect and sign in with Google.
Callbacks `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`
are allowed by default. Organisation Owners can add it once for everyone, set its tools to Always
allow (they are all reads), and block the built-in Gmail connector
([runbook](docs/runbooks/claude-org.md)).

### nexos.ai

nexos's custom MCP authenticates each person with their own API key, so set
`MCP_GUARD_PERSONAL_KEYS=true`. Each person opens `https://<guard>/keys`, signs in with Google and
gets a key, which they paste into nexos. The same deployment keeps serving Claude over OAuth.
Steps: [docs/nexos-setup.md](docs/nexos-setup.md).

### Other MCP clients

Any client that supports Streamable HTTP and MCP OAuth works once its callback is added to
`MCP_GUARD_CLIENT_REDIRECT_URIS`; until then its registration is refused (HTTP 400 on
`/register`). Setting the variable replaces the default list, so repeat Claude's entries:
```
MCP_GUARD_CLIENT_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback,http://localhost:*/callback,http://127.0.0.1:*/callback,<other client's callback>
```
Before allowing a client, check its callback is a fixed URL owned by that vendor; a wildcard host
would let any site register. Clients that only accept a fixed API key can use personal keys.

## 5. After deploying: check it works

```bash
curl https://<guard>/health                       # 200 {"status":"ok","upstream":"gmail"}
curl -i -X POST https://<guard>/mcp               # 401 with a WWW-Authenticate header
curl https://<guard>/.well-known/oauth-protected-resource/mcp   # scopes: openid, userinfo.email, gmail.readonly
```
Then add it in Claude Code, sign in, and call `list_labels`; check the audit line shows your email
with `"outcome": "ok"`. Ask the agent to send or trash a mail: the tool doesn't exist for it, and
a direct call is audited as `"decision": "deny"`. With someone outside example.com (if the consent
screen allows it at all), tools/list should say "Only example.com accounts can use this server."
With personal keys on, make a key on `/keys`, call the guard with it, revoke it and check the next
call gets 401.

## Open items

- Google OAuth client and project (step 1), and the Workspace admin's API controls; confirm what
  Gmail MCP needs if it's ever used.
- First end-to-end test against real Google sign-in, with Claude.ai, Claude Code and nexos.ai.
- Redis (or a volume for one replica), and the storage encryption key in the secret store.
- A GitLab CI pipeline building the image with the shared `build-image` component, like
  `internal-mcp`; only GitHub Actions exists today.
- Exposed directly or behind the MCP gateway.
- nexos.ai: which header it sends the key in (both `Authorization: Bearer` and `X-API-Key` are
  accepted), and whether its "Test connection" works before a key exists.
