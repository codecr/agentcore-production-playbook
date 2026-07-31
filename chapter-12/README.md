# Chapter 12: The Operations Playbook

This folder accompanies Chapter 12 of *AgentCore in Production: The Operator's Playbook* — the final chapter.

## What's here

```
chapter-12/
├── checklist/
│   └── production-launch-checklist.md   # Phase 0 through Phase 4, plus the money section
├── payments/
│   ├── budget_guard.py                  # Aggregate spend control across sessions
│   └── payment-policies.cedar           # 7 Cedar policies for payment-capable tools
├── runbook/
│   └── agent-incident-runbook.md        # Incident template, by symptom
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

**Cedar policies for payment tools.** Seven policies covering explicit payment
authorization, production-only spending, per-call ceilings by agent class, a
payee allowlist enforced at the Gateway, mandatory human approval for anything
with a duration, an out-of-hours restriction for larger amounts, and mandatory
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
| Cedar policies | May this principal spend at all, on what, in which environment? | Gateway, before the call |
| AgentCore Payments limits | How much may be spent in **this session**? | Payment infrastructure |
| BudgetGuard | How much may be spent **today and this month**? | Your code, atomic counters |

The incident that opens Chapter 12 passed the second, was never subjected to
the first, and had no third.

## Payments preview status

AgentCore Payments entered preview on **May 7, 2026**, built with Coinbase and
Stripe. As of this writing:

- Wallets: **Coinbase CDP** or **Stripe Privy**, connected as a payment
  connection; the end user must explicitly authorize the agent
- Protocol: **x402** over HTTP 402, with additional protocols on the roadmap
- Settlement: stablecoin (USDC on Base and Solana)
- **The agent never holds private keys**
- Spending limits are **per session** and time-bound
- The Coinbase x402 Bazaar MCP server is available through AgentCore Gateway,
  exposing on the order of ten thousand paid endpoints
- Region-limited, and the Stripe leg was US-only at launch

Treat it as the fastest way to build a working prototype on Bedrock, and read
Chapter 12 Section 12.6 before treating it as the production answer for a
regulated workload.

## License

Code, policies, and templates in this folder are MIT licensed. The
accompanying book chapter is copyrighted; the material is explicitly intended
for reuse — copy the checklist into your repository and edit it.
