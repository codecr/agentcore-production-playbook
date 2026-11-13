# Chapter 13 — The Operations Playbook: Day One, and the Frontier

This folder accompanies Chapter 13 of *AgentCore in Production: The Operator's Playbook* — the final chapter.

## What's here

```
chapter-13/
├── checklist/
│   └── production-launch-checklist.md   # Phase 0 through Phase 4, plus the money section
├── payments/
│   ├── budget_guard.py                  # Aggregate spend control across sessions
│   ├── payment-policies.cedar           # 7 Cedar policies for payment-capable tools
│   ├── daily-spend.dogwood                   # Dogwood temporal fence: $40 of pay_endpoint per policy session per 24 h
│   └── daily-purchase.dogwood                # the same fence for purchase_access: $10
├── runbook/
│   ├── agent-incident-runbook.md        # Incident template, by archetype and symptom
│   └── kill-switch.md                   # Stop one agent in under a minute
└── README.md
```

## What this delivers

**The launch checklist**, organized by phase rather than by service, because
the order matters more than the list. Items marked **[hard to reverse]** are
the ones that cost days now and quarters later: the identity model, the tenant
isolation model, the Gateway as sole ingress, the model as configuration, and
instrumentation from the first turn. The final section applies only if the
agent handles money, and exists because the session-scoped spending limit is
necessary and not sufficient.

**Aggregate budget enforcement.** AgentCore Payments enforces spending limits
per session at the infrastructure layer. A workload that opens two thousand
sessions a month has two thousand budgets that sum to a number nobody chose.
`budget_guard.py` adds the tiers AgentCore does not: daily and monthly caps
with atomic, conditional DynamoDB writes (the `record()` write is tied to the
same cap `authorize()` checked, so a concurrent write that would blow the
cap loses the race with `ConditionalCheckFailedException` rather than
overspending silently), a single-transaction ceiling, a payee allowlist, a
human-approval threshold, a circuit breaker that flags a single transaction
far above an agent's trailing spend pattern, and a check for whether the
tenant has already purchased the durable resource the agent is about to buy
again.

It validates policies for internal consistency, and rejects unsound ones:

```bash
python payments/budget_guard.py
#   single tx cap : $2.00
#   daily cap     : $40.00   (20 max transactions/day)
#   monthly cap   : $900.00  (binds at day 23 of sustained max spend)
#
# An unsound policy for comparison:
#   - single_tx_cap should be at most a tenth of daily_cap...
#   - monthly_cap can never be reached given daily_cap — it is decoration
#   - allowed_payees is empty...
```

**Cedar and Dogwood policies for payment tools.** Seven Cedar policies covering
explicit payment authorization, production-only spending, per-call ceilings by
agent class, a payee allowlist enforced at the Gateway, a procurement role
(an identity claim, not a model-typed token) for anything with a duration, an out-of-hours restriction for larger amounts, and mandatory
tenant attribution on every payment. Deploy them the way Chapter 3 recommends:
`LOG_ONLY`, read the decisions, then `ENFORCE`.

**An incident runbook** organized by symptom rather than by component, because
agent incidents arrive as customer complaints and finance questions rather
than as alarms. Includes a containment table ordered by speed with
reversibility marked, and a postmortem requirement that every incident
produces at least one golden-set case.

## The three-layer spend control

The chapter's argument in one table. None of the three substitutes for the
others:

| Layer | Question | Where enforced |
|---|---|---|
| Cedar + Dogwood policies | May this principal spend at all, on what, and how much in 24 h? | Gateway, before the call |
| AgentCore Payments limits | How much may be spent in **this session**? | Payment infrastructure |
| BudgetGuard | How much may be spent **today and this month**? | Your code, atomic counters |

The incident that opens Chapter 13 passed the second, was never subjected to
the first, and had no third.

The Dogwood fence counts per *policy session*. Derive the session id in your
backend from agent, tenant, and UTC date, or it bounds nothing across
sessions; and it resets whenever any temporal policy is deployed.

## AgentCore Payments status (October 2026)

AgentCore Payments entered preview on May 7, 2026 and became **generally
available on August 18, 2026**.

- Wallets: **Coinbase CDP** or **Stripe Privy**. Coinbase now requires the
  AWS Marketplace subscription "Coinbase Wallets for AgentCore Payments"
  (otherwise `SubscriptionRequiredException`) and is billed on the AWS
  invoice. Quick Create in the console provisions Coinbase credentials.
- Protocols: x402 (`exact`, plus `upto` for metered payments up to a
  ceiling) and the **Machine Payments Protocol (MPP)**.
- Twelve Regions at GA (up from four in preview). Check the AgentCore
  Regions table; São Paulo, Mumbai, Tokyo, and Seoul were not on it when
  this README was updated.
- No AgentCore charge for Payments; the wallet provider's per-operation fee
  is the real floor (about $0.005 at Coinbase CDP's published rate in mid
  2026), not on-chain gas.
- Session limits (`maxSpendAmount`, expiry 15 to 480 minutes) are enforced
  by the service before signing. The agent never holds keys; the end user
  must explicitly authorize the agent in the wallet hub.
- A curated Coinbase Bazaar of x402 endpoints is available as an MCP server
  through AgentCore Gateway.

The Cedar file was run against a live Gateway on 2026-11-10: P01, P03, and P06 enforced as described (a 201-cent call was denied, and a 150-cent call outside the window), and the policies that read principal tags are now scoped to OAuth users. `budget_guard.py` is validated offline (`repo/_verify`); `daily-spend.dogwood` and `daily-purchase.dogwood` ran against a live Gateway on 2026-11-12 with smaller caps: the sum includes the current request, an open principal fails strict validation (both are scoped to OAuth users), and Dogwood would not add the two tools' sums in one condition, so each tool has its own cap. Payments itself, the wallet calls, and `delete-payment-session` were not run.

## License

Code, policies, and templates in this folder are MIT licensed. The
accompanying book chapter is copyrighted; the material is explicitly intended
for reuse — copy the checklist into your repository and edit it.
