# Decisions and questions for leadership

What we need decided or answered before rollout. The guard is built and tested locally; everything
below is about policy, ownership and the real environment. Background:
[findings.md](findings.md) (research) and [DEPLOY.md](../DEPLOY.md) (deployment).

## Decisions

Each with the options and our recommendation.

**D1. Is Anthropic's tool blocking enough for Gmail, or must the Google permission itself be
read-only?**

- (a) Claude's built-in Gmail connector with write tools [Blocked](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities#h_9fa8289074): no code, but the Google grant
  behind it [can still send](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors#h_60ffa285c8).
- (b) The guard: the Google token is `gmail.readonly`, so Google refuses any change.
- **Recommended: (b).** It is also the only option that covers nexos.ai, which [can't limit tools](https://docs.nexos.ai/workspace/integrations#activate-or-deactivate-an-integration).

**D2. How does the guard reach Gmail?**

- (a) Gmail API (generally available).
- (b) Google's Gmail MCP server ([Developer Preview](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server#prerequisites): enrolment, preview terms, an AI-agent OAuth
  client, prompt screening).
- (c) Buy: a [Cloudflare MCP portal](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/) in front of Gmail MCP.
- **Recommended: (a)**, the default. Switching to (b) later is a config change plus Google-side
  setup.

**D3. Payhawk**

- (a) Ask Payhawk to [allow our proxy's sign-in address](https://payhawk.com/help/connecting-payhawk-mcp#supported-ai-assistants) (keeps per-person permissions).
- (b) A shared read-only Payhawk API key.
- (c) No proxy: Payhawk in Claude only, write tools Blocked.
- **Recommended: (c)** if only Claude needs Payhawk; **(a)** if nexos does.

**D4. Do we accept that read-only Gmail still exposes mail to prompt injection?**

- An agent reading a malicious email can be steered into copying it out through another tool
  that writes or fetches (another connector, web access). The guard can't stop that.
- **Recommended: accept, with a rule:** agents with Gmail access get no tool that sends data out,
  or that tool needs approval.

**D5. Personal keys for nexos.ai**

- Each key reads a whole mailbox and is kept by nexos. Default lifetime 180 days.
- **Recommended:** keep 180 days for the pilot; revisit after.

## Questions, by who can answer

The four that change the plan most: **1**, **6**, **10** and **17**.

### Leadership / security

1. Which of D1-D5 above do you agree with? Who signs off?
2. Does a new internal tool that handles email (and later finance) data need a **security review**, and who runs it?
3. Where may we host an internal, internet-reachable service (Cloud Run, AWS, other), and must it sit behind the company **MCP gateway**?
4. Where do secrets and stored sign-ins live (Secret Manager, Vault, Redis), and must the audit log go to a central SIEM?

### Claude admins (Owners)

5. **Team or Enterprise**, and how many seats? (The onboarding email says Team; Enterprise adds [custom roles](https://support.claude.com/en/articles/13930452-manage-custom-roles-on-enterprise-plans) and the [verified-domain restriction](https://support.claude.com/en/articles/15402193-restrict-verified-domain-connectors-to-your-enterprise).)
6. Will you add the guard as an org **custom connector** and **block the built-in Gmail connector**?
7. Are member [connector requests](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities#h_9213598058) on, and can they be declined by default?
8. Do people use Claude Code with the company Claude account or with API keys? (API-key sessions [skip the org's connector controls](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claude-ai).)

### IT / device management

9. Are laptops **managed** (Jamf, Intune), so IT can push Claude Code and Claude Desktop policy? Is a personal Claude account with work data allowed?

### Google Workspace admins

10. Will you **block "Claude for Gmail"** and nexos.ai's Gmail app at Google, and allow the guard's OAuth client for read-only Gmail ("Specific Google data")? Can we pilot with one group first?
11. Who creates and owns the **Google Cloud project and Internal OAuth client** for the guard?
12. Do we have more than one Workspace domain? The guard accepts one, matched against the account's Workspace domain (`hd`).
13. Should the guard's OAuth client be created with **Use this client for an AI-powered agent**
    ticked? The default Gmail API mode doesn't need it, but [Google's Gmail MCP does](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server#configure-mcp-client), and it
    [labels agent traffic for admin controls](https://support.google.com/cloud/answer/15549257#agentic-client). A
    [third-party project](https://github.com/taylorwilsdon/google_workspace_mcp#:~:text=it%20can%27t%20be%20changed%20on%20an%20existing%20client%20after%20November%2012%2C%202026)
    says the setting can't be changed on an existing client after **November 12, 2026** (Google's docs give no date); if so, switching to
    Gmail MCP later means a new client.

### nexos.ai admins

14. Which nexos plan, and who is the nexos Owner?
15. Can the built-in Gmail integration be switched off once the guard is live?
16. Ask nexos: does custom MCP support **OAuth** (then personal keys could go), which **header** carries the API key, what are their **egress IPs**, and does a key entered when adding the server become a **default for members** who haven't added their own?

### Payhawk owners / finance

17. Is Payhawk needed in **nexos**, or only in **Claude**? (Only Claude: no proxy needed.)
18. If nexos needs it: will someone ask Payhawk Support to allow our proxy's sign-in address, or is a shared read-only API key acceptable?
