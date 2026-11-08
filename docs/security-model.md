# Security model of the guard

What the guard guarantees, how, and what it does not cover. The research behind the design
(Claude and nexos.ai controls, Gmail scopes, Workspace controls) is in
[findings.md](findings.md).

## Layers that keep Gmail read-only

| Layer | Enforced by | Holds even if... |
|---|---|---|
| 1. The Google token holds only `gmail.readonly` (plus sign-in scopes) | Google | the guard has a bug, or a client calls Gmail some other way with that token |
| 2. Any token holding more is refused, and revoked when the guard obtained it | Guard (`ScopeLimitedTokenVerifier`), on every request | Google grants more than was asked for (an old grant, a misconfigured consent screen) |
| 3. Only allowlisted tools are listed or callable | Guard (FastMCP `Visibility`: deny all, then allow the listed names) | the upstream adds tools, or a client calls a name it wasn't shown |
| 4. A pinned tool whose definition changed is dropped | Guard (`Pins`), for `mcp` upstreams | a read tool quietly gains a write parameter |
| 5. The Gmail API tools only make GET requests, with checked ids | Guard (`gmail_api`) | an argument tries to reach another endpoint |
| 6. `guard.yaml` accepts only `.readonly` scopes | Guard, at startup | someone edits the config to ask for more |
| 7. The OAuth client is restricted to `gmail.readonly` at Workspace level | Google, configured by a Workspace admin | someone changes the guard's code |

Layer 1 is the real guarantee: Google rejects any send, draft, label or delete made with the
token, whatever the client does. The sign-in asks for exactly `openid`, `userinfo.email` and
`gmail.readonly`, with `include_granted_scopes=false`. Every token is then checked with Google's
tokeninfo: it must have been issued to the guard's own OAuth client and hold the required scopes
and nothing else. A sign-in whose token fails this fails at the token exchange (the client gets
an OAuth error) and the grant is revoked at Google. A token pasted in `--dev` is refused the same
way but not revoked, since it's the person's own.

## Who gets in

- **Only the company's Workspace accounts.** The Internal consent screen blocks other accounts at
  Google. The guard also checks, on every request: the email is verified, ends in the allowed
  domain, and Google's `hd` (the Workspace domain) equals it. `hd` is required, not just checked
  when present: anyone can make a personal Google account for an address like
  `pat@example.com` and verify it, but such an account has no `hd` and isn't governed by the
  company's Workspace (its admin can't suspend it, offboarding doesn't touch it).
- **Only Claude's clients sign in with OAuth.** MCP clients may register only the callbacks in
  `MCP_GUARD_CLIENT_REDIRECT_URIS` (Claude.ai, Claude Desktop and Claude Code by default), so
  another app can't collect people's Gmail access through the guard. The guard's consent page
  ("Claude Code wants to access gmail-guard") shows once per client and browser, and is asked
  again on cross-site navigations, against confused-deputy attacks.
- **Personal keys** (nexos.ai) need a fresh Google sign-in on `/keys`, with the same scope and
  domain checks; each person has at most one key.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| Token passthrough (forwarding the MCP client's credential upstream, forbidden by the MCP spec) | The upstream client is built per request with only the person's Google token, and `forward_incoming_headers=False`: FastMCP's proxy client would otherwise forward the agent's request headers (a personal key in `X-API-Key`, cookies). A test checks the Gmail MCP upstream sees neither |
| Google tokens sent to a third party | `mcp` upstreams must be `*.googleapis.com` hosts (config is refused otherwise) |
| Upstream schema drift | Optional pins over name, description and input schema; drift drops the tool and logs the new hash |
| Untrusted upstream annotations (`readOnlyHint` lies) | Decisions use only the allowlist; the `gmail-mcp` allowlist was checked against Google's annotations by a test, but a lying annotation can't add a tool |
| Resources, prompts from the upstream | Never offered: the allowlist only lets tools through |
| Stolen personal key | Random 256-bit keys, stored only as sha256 hashes, expire after 180 days, revocable on `/keys` (or replaced by making a new one), give access only to the owner's mailbox, read-only |
| Stolen storage | Everything stored (OAuth state, Google tokens, key records) is Fernet-encrypted with `MCP_GUARD_STORAGE_ENCRYPTION_KEYS`; keys are hashes only; FastMCP stores its own refresh tokens by hash |
| Login CSRF on `/keys` | PKCE (S256); a random `state` stored server-side for 10 minutes and bound to the browser by an HttpOnly cookie, so a callback link started in another browser is refused |
| DNS rebinding against a local guard (`--dev`) | Host and Origin are checked when the guard listens on localhost (421 otherwise) |
| One agent using up the shared Gmail quota | Per-person rate limit (2 requests/s, bursts of 20 by default) |
| Oversized responses | Results cut at 1 MB; Gmail bodies at 20,000 characters |
| Leaking mail into logs | The audit line has person, tool, decision, outcome and duration only; never arguments, results or tokens |

## Not covered (yet)

- **Prompt injection in mail content.** Email bodies are untrusted input to the model. The guard
  can't send mail, so injected instructions can't exfiltrate through Gmail itself, but they can
  still steer the model's other tools in the same chat. Google asks for prompt-injection screening
  (e.g. Model Armor) in front of its own Workspace MCP servers; screening responses here is a
  possible follow-up.
- **Argument checks.** The allowlist is by tool name. Every allowed tool is read-only whatever its
  arguments, so that is enough today.
- **Payhawk** isn't wired: an `mcp` upstream receives the person's Google token, and Payhawk needs
  its own per-person sign-in, which needs Payhawk Support to allowlist the guard's redirect URI
  ([runbooks/payhawk.md](runbooks/payhawk.md)).
- **Revoking at Google when a key is revoked.** Revoking a personal key deletes its stored Google
  token but doesn't revoke the grant at Google, which would also sign the person out of Claude
  (same OAuth client). To cut everything off, remove the app at Google (the person under
  Third-party apps, or an admin under the user's Security settings).

## Verified locally vs. needs real accounts

Verified on localhost with a fake Google (OAuth, tokeninfo, userinfo, revocation, Gmail API, Gmail
MCP), in tests and by hand: Claude Code's real sign-in flow through the guard's consent page and
its use of the six tools; the same flow from FastMCP's OAuth client in tests; refusal and
revocation of over-broad grants at sign-in; the domain and `hd` checks; personal keys (create, use
as bearer or `X-API-Key`, replace, revoke, browser binding, hashed and encrypted at rest); a
sign-in made on one replica used on another through Redis; no header forwarding to Gmail MCP;
audit lines; pins; size cap; rate limit; the container image.

Verified against real Google in `--dev`, with an OAuth Playground token on a personal account:
Google's real tokeninfo and userinfo work with the token check (a token without `gmail.readonly`
was refused; a read-only one accepted), the Gmail API tools read real labels and threads, and the
write tools were refused without reaching Gmail.

Needs a real Google OAuth client and Workspace tenant: that Google's real refresh and revocation
behave as the fake does (the fake follows Google's documented responses); the `hd` check on a real
Workspace account;
the Internal consent screen; behaviour when a Workspace admin sets the client to "Specific Google
data"; real Gmail API responses for unusual messages. Needs Claude.ai: the custom connector's
sign-in (its callback is allowed). Needs nexos.ai: the header it sends the key in, and its "Test
connection".
