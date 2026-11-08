# Security model of the proxy

What the proxy guarantees, how, and what it does not cover. The research behind the
design (Claude and nexos.ai controls, Gmail scopes, Workspace controls) is in the
findings docs.

## Layers that keep Gmail read-only

| Layer | Enforced by | Holds even if... |
|---|---|---|
| 1. The Google token holds only `gmail.readonly` | Google | the proxy has a bug, or a client calls Gmail some other way with that token |
| 2. Only allowlisted, pinned tools are listed | Proxy (`PolicyProvider`) | the upstream adds or changes tools |
| 3. Every call is re-checked: allowlist, current pin, arguments against the pinned schema | Proxy (`PolicyMiddleware`) | a client skips `tools/list` or guesses names |
| 4. Gmail adapter issues only GET requests, with validated IDs | Proxy (`GmailApi`) | an argument tries to reach another endpoint |
| 5. OAuth client restricted to `gmail.readonly` at Workspace level | Google, configured by [GWA] | someone changes the proxy's requested scopes |

Layer 1 is the real guarantee: Google rejects any send, draft, label or delete made with
the token, whatever the client does. The proxy requests exactly
`openid email gmail.readonly` with `include_granted_scopes=false`, then verifies the
granted set in the token response, again through Google's tokeninfo endpoint, and again
on every refresh. Any extra scope, write or not, makes it refuse the grant and revoke it
at Google, so a broader token is never stored.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| Token passthrough (forwarding the MCP client's credential upstream, forbidden by the MCP spec) | The proxy never forwards inbound headers. The `mcp` upstream builds its own transport with only configured credentials and refuses to run if the client ever defaults to forwarding (`forward_incoming_headers`). A test proves an HTTP upstream sees neither `Authorization`, `X-API-Key` nor cookies from the client |
| Upstream schema drift (a read tool gains a write parameter) | Definition pins over name, description and input schema; drift hides the tool, blocks calls and logs `tool_hidden` |
| Untrusted upstream annotations (`readOnlyHint` lies) | Annotations are rewritten from config; config rejects `readOnlyHint: false` / `destructiveHint: true`; upstream hints are ignored for decisions |
| Resources, prompts, sampling, elicitation, roots from the upstream | Never relayed: the proxy only registers tools, its upstream client has no sampling/elicitation/roots handlers, and resource/prompt requests are denied |
| Stolen personal key | Keys are random 256-bit values, stored as sha256 hashes, expire (default 180 days), are revocable per key or all at once, and give access only to the owner's mailbox, read-only. `last_used_at` is shown on the keys page |
| Stolen database | Refresh tokens are AES-256-GCM encrypted with keys from Secret Manager, bound to the user's `sub`; keys are hashes only |
| Accounts from outside the organization | ID token verified against Google's JWKS (issuer, audience, expiry, nonce); `hd` must equal the Workspace domain; email must be verified. The Internal consent screen blocks outside accounts at Google too |
| Login CSRF and code injection | PKCE (S256), `state` and `nonce` bound to the browser session, 10-minute login window |
| CSRF on key management | Per-session CSRF tokens, `SameSite=Lax` signed session cookie, `Origin` checked against the public origin |
| DNS rebinding | Requests with a `Host` other than the public host get `421` |
| Clickjacking, script injection on the web pages | `frame-ancestors 'none'`, nonce-based CSP, all output HTML-escaped |
| Oversized responses | Per-call size cap (default 1 MiB, per tool configurable); Gmail bodies truncated (default 20,000 characters) |
| Leaking mail into logs | Audit lines carry an argument hash, never arguments, results or tokens; errors the proxy itself returns to clients are fixed strings |

## Not covered (yet)

- **Prompt injection in mail content.** Email bodies are untrusted input to the model.
  The proxy cannot send mail, so injected instructions cannot exfiltrate through Gmail
  itself, but they can still steer the model's other tools in the same chat. Google
  requires prompt-injection screening for its own Workspace MCP servers; adding a
  screening step (for example Model Armor) on responses is a possible follow-up.
- **Rate limiting** per user or key is not implemented; Cloud Run max instances and
  Gmail API quotas are the current limits.
- **Claude web, desktop and mobile** need an MCP OAuth authorization-server mode, not
  built yet. On Claude, rely on Anthropic's server-side **Blocked** policy for the
  built-in Gmail connector meanwhile; that is enforced by Anthropic but the built-in
  connector's token can still send, so the Google-side controls [GWA] matter.
- **Payhawk** (an `mcp` upstream with per-user OAuth) is not wired: Payhawk accepts
  OAuth sign-ins only from allowlisted apps, and per-user upstream OAuth is not built yet.
  The proxy sends `mcp` upstreams no credential; a shared upstream credential is an open
  stakeholder decision ([open questions](open-questions.md)).

## Verified locally vs. needs real accounts

Verified end to end on localhost, with the fake Google and with Claude Code and the
FastMCP client as MCP clients: Google sign-in flow (PKCE, ID token verification, `hd`
check, scope checks and revocation of over-broad grants), personal keys (create, use as
bearer or `X-API-Key`, revoke, disconnect), tool listing with rewritten annotations,
Gmail reads, unknown/write tools rejected without reaching Gmail, pin drift, size cap,
no header forwarding to an HTTP upstream, the container image, and the store on SQLite
and PostgreSQL.

Needs a real Google OAuth client and Workspace tenant to verify: that Google's real
token, tokeninfo and refresh responses report scopes as the fake does; the consent screen
an Internal app shows; behaviour when a Workspace admin sets the client to "Specific
Google data"; real Gmail API responses for unusual messages (nested multiparts,
non-UTF-8 bodies, very large threads). Needs nexos.ai access: the header nexos sends the
key in and its "Test connection" behaviour.
