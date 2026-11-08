# Open questions

Decisions that would change the recommendation in [findings.md](findings.md). Sources checked on 2026-11-08.

The concrete questions to ask each admin group, which feed these decisions, are in [questions-for-stakeholders.md](questions-for-stakeholders.md).

## 1. Is Anthropic-enforced read-only enough for Gmail, or do you need a credential-level guarantee?

- **(a)** Built-in connector with write tools Blocked: no code, but the token can send ([findings 8.2](findings.md#82-lighter-option-built-in-connector-anthropic-enforced-read-only)).
- **(b)** Read-only proxy with `gmail.readonly` and Anthropic's Gmail app blocked at Google: recommended ([findings 8.1](findings.md#81-target-real-read-only-guarantee-proxy-route-team-or-enterprise)).

## 2. Gmail upstream for the proxy

- **(a)** Native Gmail API adapter: recommended; GA API, we own five read tools.
- **(b)** Proxy Google's Gmail MCP server: Developer Preview; needs program enrollment.
- **(c)** Buy instead of build: Cloudflare MCP server portal in front of Google's Gmail MCP (and Payhawk MCP), allowlist mode, upstream scope `gmail.readonly` ([build vs buy](proxy-design.md#reuse-an-existing-gateway-or-build)).

## 3. Payhawk

- **(a)** Ask Payhawk Support to allow the proxy redirect URI: per-user permissions kept; recommended if nexos needs Payhawk.
- **(b)** Read-only Payhawk API key: one shared account-wide credential.
- **(c)** No Payhawk proxy: direct custom connector in Claude with write tools Blocked; Claude only.

## 4. Facts that change details, not direction

- Which Claude plan (Team or Enterprise)? Believed to be Team (from the onboarding email), pending confirmation. Enterprise adds custom roles, the verified-domain restriction and Desktop policy without MDM.
- Are devices MDM-managed? Without MDM, Claude Code sessions on API keys or the nexos gateway escape server-managed MCP controls.
- Will the Google Workspace admins agree to block "Claude for Gmail" and nexos's Gmail app, and to configure the proxy client as Specific Google data?
- Does nexos support OAuth for custom MCP? If yes, drop the personal-key mode.
- Which nexos plan? Plan pages weren't readable; no doc ties integration controls to a plan.
