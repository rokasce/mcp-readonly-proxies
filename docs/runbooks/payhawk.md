# Runbook: Payhawk MCP

For Claude Owners and Payhawk admins: give AI assistants read-only Payhawk access. Background: [findings.md](../findings.md#65-payhawk-mcp). Checked on 2026-11-08 against Payhawk's [About](https://payhawk.com/help/payhawk-mcp-overview.md), [Connecting](https://payhawk.com/help/connecting-payhawk-mcp.md), [Tool reference](https://payhawk.com/help/payhawk-mcp-tool-reference.md), [FAQ](https://payhawk.com/help/faq-on-the-payhawk-mcp.md), [Troubleshooting](https://payhawk.com/help/troubleshooting-the-payhawk-mcp.md), [Developer API](https://payhawk.com/help/overview-of-the-payhawk-developer-api.md).

## Key facts

- **Server:** `https://mcp.payhawk.com/mcp`. Each user signs in with their own Payhawk login, so Payhawk roles apply. Sessions last 7 days unused, 30 at most.
- **Only listed apps can sign in** (Claude, Claude Code, ChatGPT, Cursor and others); others fail with `host-not-allowed`. **nexos.ai is not listed.**
- **Write tools** need user confirmation (a prompt, not a read-only mode). The MCP "never starts a payment or a transfer".
- **API keys** are Full access or Read-only, account-wide, 15 requests/s.

![Payhawk accepts sign-ins only from the listed AI apps; nexos.ai is not listed](../images/payhawk-supported-clients.png)

*Source: [Payhawk Help Center, Connecting Payhawk to your AI Assistant](https://payhawk.com/help/connecting-payhawk-mcp.md), captured 2026-11-08.*

## Options

| Option | Covers | Per-user permissions | Status |
|---|---|---|---|
| **A. Direct custom connector in Claude, write + ambiguous tools Blocked** | Claude only | Kept | Works today, no code |
| **B. Payhawk proxy with per-user OAuth** | Claude, Claude Code, nexos | Kept | Waits on **Payhawk allowlisting the proxy redirect URI** |
| **C. Payhawk proxy on a Read-only API key** | Claude, Claude Code, nexos | **Lost**: one shared key sees everything | Works today; not recommended |

**Recommendation:** Option A if only Claude needs Payhawk. Option B for nexos or a credential-level guarantee.

## Option A steps (Claude only)

1. Organization settings > Connectors > Add custom connector: URL `https://mcp.payhawk.com/mcp`, OAuth client "Register automatically".
2. In its tool permissions, set every **Write** and **Ambiguous** tool (table below) to **Blocked**. Read tools: Always allow or Needs approval.
3. Members connect with their own Payhawk login.
4. For Claude Code sessions not logged in to claude.ai, see [claude-code-managed-settings.md](claude-code-managed-settings.md).

## Option B steps (proxy)

1. **Start early:** email support@payhawk.com asking them to allow the proxy's redirect URI (e.g. `https://payhawk-ro.example.com/auth/callback`).
2. Once allowed, deploy the proxy in front of `https://mcp.payhawk.com/mcp` ([proxy-design.md](../proxy-design.md#payhawk)).
3. Call `tools/list` once and record each tool's `annotations` and input schema.
4. Allowlist only the Read tools below; everything else is denied.
5. If Payhawk declines, choose Option C or Option A.

## Tool classification (82 tools)

**Inferred from the names** plus Payhawk's rule that read tools "never change anything". **Deny anything unclear.**

| Class | Tools |
|---|---|
| **Read** (allow) | `list_expenses`, `get_expense`, `get_expense_approval_status`, `list_expense_payments`, `list_documents`, `get_invoice_inbox`, `list_close_blockers`, `list_expense_reports`, `get_expense_report`, `list_expense_subscriptions`, `list_mileage_claims`, `list_cards`, `get_card`, `list_card_requests`, `get_card_request`, `list_fund_accounts`, `get_fund_account`, `list_balances`, `list_fund_account_activity`, `get_upcoming_payments`, `get_transaction`, `get_fx_rate`, `review_queue`, `needs_my_attention`, `get_approval_request`, `list_audit_logs`, `list_suppliers`, `get_supplier`, `list_purchase_orders`, `get_purchase_order`, `list_purchase_order_types`, `list_merchants`, `get_merchant`, `list_card_acceptance_opportunities`, `list_export_history`, `list_failed_exports`, `list_export_templates`, `list_account_codes`, `list_tax_rates`, `list_custom_fields`, `list_expense_categories`, `get_expense_category`, `list_accounts`, `list_users`, `get_user`, `list_teams`, `list_team_members`, `list_spend_policies`, `get_spend_policy`, `list_comments`, `get_job_status`, `get_agent_job_status`, `product_help`, `list_playbooks`, `get_playbook` |
| **Write** (deny) | `upload_document`, `ingest_and_auto_match_receipt`, `create_expense`, `submit_expense`, `recode_expense`, `confirm_expense_subscription`, `manage_expense_subscription`, `update_card_status`, `create_card_request`, `approve_or_return`, `complete_expense_review`, `return_expense_for_review`, `out_of_office_delegate`, `manage_supplier`, `manage_purchase_order`, `run_export`, `manage_spend_policy`, `add_comment`, `submit_feedback`, `draft_playbook`, `manage_playbook` |
| **Ambiguous** (deny) | `budgets` (may read or manage), `travel_agent`, `procurement_agent`, `payments_agent` (can act), `dispatch_widget_action` (runs widget buttons), `insights_query_async` (starts background jobs) |
