# Questions for stakeholders

Questions to ask before rollout, grouped by who can answer them. Each links to the part of the findings or runbooks it relates to. The decisions these answers feed are in [open-questions.md](open-questions.md).

The four that change the plan most: 1, 6, 11 and 18.

## Claude admins (Owners)

1. Are we on Claude **Team or Enterprise**, and roughly how many seats? (The onboarding email says Team; Enterprise adds the verified-domain restriction and custom roles.)
   Related: [Team vs Enterprise controls](findings.md#33-per-tool-admin-controls-team-vs-enterprise), [Team gap](findings.md#81-target-real-read-only-guarantee-proxy-route-team-or-enterprise).
2. Is the built-in **Gmail connector** already enabled for the org, and what are its tool permissions set to now?
   Related: [Claude org runbook, Route A](runbooks/claude-org.md#route-a-built-in-gmail-connector-with-write-tools-blocked).
3. Would you set Gmail's **write/delete tools to Blocked** org-wide? Who approves that kind of change?
   Related: [Setup steps for the built-in route](findings.md#34-setup-steps-for-the-built-in-route-claude-owner), [Claude org runbook, Route A](runbooks/claude-org.md#route-a-built-in-gmail-connector-with-write-tools-blocked).
4. Are **member connector requests** on? Can they be turned off or declined by default?
   Related: [What the controls do](runbooks/claude-org.md#what-the-controls-do).
5. Would you add a **custom connector** we host (the read-only Gmail proxy) for the org?
   Related: [Claude org runbook, Route B](runbooks/claude-org.md#route-b-read-only-proxy-or-google-gmail-mcp-as-custom-connector).
6. Is Anthropic's server-side blocking **good enough for compliance**, or does security need a guarantee where the Google permission itself cannot send? This decides whether Claude needs the proxy at all.
   Related: [Residual risks of the built-in route](findings.md#83-residual-risks-of-82-why-81-is-the-guarantee), [Open question 1](open-questions.md#1-is-anthropic-enforced-read-only-enough-for-gmail-or-do-you-need-a-credential-level-guarantee).
7. How do people use **Claude Code**: signed in with the company Claude account, or with API keys? Are there managed settings?
   Related: [Which sessions each delivery method reaches](runbooks/claude-code-managed-settings.md#which-sessions-each-delivery-method-reaches).

## IT / device management

8. Are laptops **company-managed** (MDM such as Jamf or Intune), and can IT push Claude Code and Claude Desktop policy files?
   Related: [Claude Code managed settings](runbooks/claude-code-managed-settings.md#which-sessions-each-delivery-method-reaches), [Claude Desktop managed policies](runbooks/claude-org.md#claude-desktop-managed-policies).
9. Is using a **personal Claude account** with work data allowed, and is it monitored?
   Related: [Personal accounts](runbooks/claude-org.md#personal-accounts).

## Google Workspace admins

10. Under Security > API controls > **Accessed apps**: is "Claude for Gmail" listed, and with which permissions? Is nexos.ai's Gmail app listed, and with which permissions?
    Related: [Workspace admin runbook, step 1](runbooks/google-workspace-admin.md#1-record-what-each-ai-app-is-requesting).
11. Would you **block "Claude for Gmail"** (and nexos's Gmail app) at Google, or limit it to read-only? Can we test on a pilot group first?
    Related: [Workspace admin runbook, step 2](runbooks/google-workspace-admin.md#2-set-the-access-level-for-claude-for-gmail), [step 3](runbooks/google-workspace-admin.md#3-nexosais-gmail-app).
12. Can we get an **internal Google OAuth client** in a company GCP project, limited to read-only Gmail? Who owns GCP projects?
    Related: [Workspace admin runbook, step 4](runbooks/google-workspace-admin.md#4-configure-our-read-only-oauth-client), [Gmail OAuth scopes](findings.md#61-gmail-oauth-scopes).
13. Is "**Trust internal apps**" checked, and are unconfigured third-party apps allowed today?
    Related: [Workspace admin runbook, step 5](runbooks/google-workspace-admin.md#5-optional-hardening).

## nexos.ai admins

14. Which **nexos plan** are we on, and who is the nexos Owner?
    Related: [nexos.ai findings](findings.md#5-nexosai).
15. Is the built-in **Gmail integration** active today? Could it be switched off once the proxy is ready?
    Related: [nexos runbook, steps](runbooks/nexos.md#steps).
16. Does nexos custom MCP support **OAuth sign-in**, or only per-user API keys? (With OAuth, users would not need personal keys.)
    Related: [How users authenticate to the proxy](proxy-design.md#how-users-authenticate-to-the-proxy).
17. Does nexos need a fixed **IP allowlist**, or can it reach a public internet endpoint?
    Related: [Transport and hosting](proxy-design.md#transport-and-hosting).

## Payhawk owners / finance

18. Do we need Payhawk in **nexos**, or only in **Claude**? If only Claude, no proxy is needed: block the write tools.
    Related: [Payhawk options](runbooks/payhawk.md#options), [Payhawk recommendation](findings.md#9-payhawk-specific-recommendation).
19. Would someone ask **Payhawk Support** to allow our proxy's sign-in address? Payhawk only accepts sign-ins from listed apps, and nexos is not on the list.
    Related: [Payhawk Option B steps](runbooks/payhawk.md#option-b-steps-proxy).
20. Is a **shared read-only Payhawk API key** acceptable, or must everyone keep their own Payhawk permissions?
    Related: [Payhawk options](runbooks/payhawk.md#options), [How the proxy authenticates upstream](proxy-design.md#how-the-proxy-authenticates-upstream).

## Hosting and security

21. Where are we allowed to host an internal, internet-reachable service (Google Cloud Run, AWS, other)?
    Related: [Transport and hosting](proxy-design.md#transport-and-hosting).
22. Where should secrets and refresh tokens live (Secret Manager, Vault), and does the audit log need to go to a central SIEM?
    Related: [Transport and hosting](proxy-design.md#transport-and-hosting), [Logging and audit](proxy-design.md#logging-and-audit).
23. Does a new internal tool touching email and finance data need a **security review**, and who signs it off?
    Related: [Proxy design](proxy-design.md).
