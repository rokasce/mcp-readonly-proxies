# Security model of the guard

What the guard guarantees and what it doesn't. Research behind it: [findings.md](findings.md).

## Layers that keep Gmail read-only

| Layer | Enforced by | Holds even if... |
|---|---|---|
| 1. The Google token holds only `gmail.readonly` (plus sign-in) | Google | the guard has a bug |
| 2. A token holding more is refused, and revoked if the guard obtained it | Guard, every request | Google grants more than asked |
| 3. Only allowlisted tools are listed or callable | Guard | the upstream adds tools, or a client calls a hidden name |
| 4. A pinned tool whose definition changed is dropped | Guard, `mcp` upstreams | a read tool quietly gains a write parameter |
| 5. Gmail API tools only send GETs, with checked ids | Guard, `gmail_api` | an argument tries to reach another endpoint |
| 6. `guard.yaml` accepts only `.readonly` scopes | Guard, at startup | someone edits the config to ask for more |
| 7. The OAuth client is limited to `gmail.readonly` in Workspace | Google, set by a Workspace admin | someone changes the guard's code |

Layer 1 is the real guarantee: Google rejects any write made with the token. The sign-in asks for
exactly `openid`, `userinfo.email` and `gmail.readonly`, and Google's tokeninfo confirms each token
was issued to the guard's OAuth client with those scopes and no more. A token pasted in `--dev` is
refused the same way but not revoked, since it's the person's own.

## Who gets in

- **Only company Workspace accounts.** The Internal consent screen blocks others at Google, and the
  guard requires a verified email in the allowed domain with a matching Workspace domain (`hd`).
  Without `hd`, a personal Google account made for `pat@example.com` would get in, and the
  company can't suspend or offboard it.
- **Only Claude signs in with OAuth.** Clients may register only the callbacks in
  `MCP_GUARD_CLIENT_REDIRECT_URIS` (Claude.ai, Claude Desktop and Claude Code by default), so no
  other app can collect Gmail access through the guard.
- **Personal keys** (nexos.ai) need a fresh Google sign-in on `/keys` with the same checks. One key
  per person.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| Forwarding the client's credential upstream | Upstream calls carry only the person's Google token, no incoming headers. Tested |
| Google tokens sent to a third party | `mcp` upstreams must be `*.googleapis.com` |
| Upstream tool changes | Optional [pins](configuration.md#pins); a changed tool is dropped and its new hash logged |
| Lying `readOnlyHint` annotations | Only the allowlist decides, so a lying annotation can't add a tool |
| Upstream resources and prompts | Never offered |
| Stolen personal key | 256-bit, stored hashed, expires in 180 days, revocable, reads only its owner's mail |
| Stolen storage | Everything is Fernet-encrypted; personal keys and the guard's refresh tokens are stored only as hashes |
| Login CSRF on `/keys` | PKCE, plus a 10-minute `state` tied to the browser by an HttpOnly cookie |
| DNS rebinding on a local `--dev` guard | Host and Origin checked on localhost (421 otherwise) |
| One agent using up the shared Gmail quota | Per-person rate limit (2/s, bursts of 20) |
| Oversized responses | Results cut at 1 MB, bodies at 20,000 characters |
| Mail leaking into logs | The [audit line](configuration.md#audit-log) never holds arguments, results or tokens |

## Not covered

- **Prompt injection in mail.** Mail is untrusted input. It can't make the guard send anything, but
  it can steer the model's other tools in the same chat. Screening responses (like Google's Model
  Armor) is a possible follow-up.
- **Argument checks.** The allowlist is by name only; enough while every allowed tool is read-only.
- **Payhawk.** Not wired; it needs its own per-person sign-in ([payhawk.md](runbooks/payhawk.md)).
- **Revoking a key doesn't revoke at Google.** It deletes the stored Google token; revoking the grant
  would also sign the person out of Claude. To cut everything off, remove the app at Google.

## What has been verified

| Where | Verified |
|---|---|
| Locally, fake Google | Claude Code's real sign-in and the six tools; over-broad grants refused; domain and `hd` checks; personal keys end to end; Redis across replicas; no header forwarding; audit, pins, size cap, rate limit; the container image |
| Real Google, `--dev`, personal account | The token check against real tokeninfo and userinfo; real labels and threads read; write tools refused before Gmail |

Still needs real accounts:

- **Google OAuth client and Workspace:** refresh and revocation, `hd`, the Internal consent screen,
  "Specific Google data", unusual messages.
- **Claude.ai:** the custom connector's sign-in.
- **nexos.ai:** the header it sends the key in, and its "Test connection" ([nexos-setup.md](nexos-setup.md)).
