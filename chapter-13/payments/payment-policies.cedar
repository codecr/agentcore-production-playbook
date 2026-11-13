// =============================================================================
// payment-policies.cedar — Cedar policies for payment-capable tools
// =============================================================================
// Accompanies Chapter 13 of "AgentCore in Production: The Operator's Playbook".
//
// AgentCore Payments enforces spending limits per session at the
// infrastructure layer. Cedar answers a different question at the Gateway:
// whether this principal, in this session, may invoke a payment-capable tool
// at all, and under what conditions.
//
// The two controls compose exactly the way schemas and Cedar composed in
// Chapter 9:
//
//     Payments limits  — how much may be spent in this session
//     Cedar policies   — who may spend, on what, in which environment
//     BudgetGuard      — how much may be spent this day and this month
//
// None of the three substitutes for the others. The incident in Section 13.1
// passed the session limit, was never subjected to Cedar, and had no third.
//
// 2nd edition (October 2026): AgentCore Policy also accepts Dogwood, the
// temporal superset of Cedar. The daily per-session spending fence lives in
// daily-spend.dogwood next to this file; cedarpy cannot evaluate it.
//
// These tools assume payment-capable calls are exposed as Gateway targets
// (payments___pay_endpoint, payments___purchase_access). An agent that pays
// through an in-process HTTP plugin instead never reaches the Gateway, and
// none of these policies see it.
//
// Deploy with the same discipline as Chapter 3: LOG_ONLY first, read the
// decision logs, then ENFORCE.
//
// Principal scope (live-tested 2026-11-10): every forbid that reads a principal
// tag is scoped to `principal is AgentCore::OAuthUser`. Left open, the analyzer
// reports it as Overly Restrictive for AgentCore::IamEntity (IAM callers carry
// no tags), which fails FAIL_ON_ANY_FINDINGS. Create P01 first: a forbid added
// to an engine with no permit is reported the same way.
// =============================================================================


// -----------------------------------------------------------------------------
// P01 — Payment tools require an explicitly payment-authorized session
// -----------------------------------------------------------------------------
// The end user authorizes the agent to use their wallet. That authorization
// must be visible as a claim on the principal, and no payment tool runs
// without it. A session that was never authorized to spend cannot spend,
// regardless of what limits are configured downstream.

permit (
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"payments___pay_endpoint",
    AgentCore::Action::"payments___purchase_access"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
when {
  principal.hasTag("payment_authorized") &&
  principal.getTag("payment_authorized") == "true" &&
  principal.hasTag("wallet_id")
};


// -----------------------------------------------------------------------------
// P02 — Payments only in production, never from a non-production session
// -----------------------------------------------------------------------------
// A staging agent that can spend real money is a staging agent that will,
// on the day someone points it at a production endpoint by accident. The
// session environment is set by Identity at session start; the agent cannot
// change it.

forbid (
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"payments___pay_endpoint",
    AgentCore::Action::"payments___purchase_access"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
unless {
  principal.hasTag("session_environment") &&
  principal.getTag("session_environment") == "production"
};


// -----------------------------------------------------------------------------
// P03 — Per-call amount ceiling by agent class, at the Gateway
// -----------------------------------------------------------------------------
// A constraint, so a forbid (Chapter 3, 2nd edition: grants are permits,
// constraints are forbids). The 1st edition wrote this as a permit, and P01
// already permitted the same action without any ceiling; Cedar permits are
// OR'd, so the ceiling never bound anything.
//
// Who wrote each value?
//   * the ceilings: this policy;
//   * agent_class: a claim the identity provider puts on the principal;
//   * amount_cents: the model, as the tool's argument. That is acceptable
//     only because it is the action's own parameter: the payment tool must
//     settle exactly this amount and refuse any 402 quote above it (x402
//     `exact` price, or the `upto` ceiling). If the tool could pay more than
//     the argument says, this policy would rest on a model-written value.
// The ceilings are deliberately low: these are micropayments, and any single
// call asking for more than a couple of dollars is either a mistake or a
// subscription.
//
// 2nd edition: amounts are integer cents (Cedar Long), declared as an integer
// in the tool's input schema. The 1st edition used a Cedar decimal, which
// required the caller to send Cedar's __extn JSON encoding inside tool
// arguments; integer cents avoid that and match daily-spend.dogwood, whose
// `sum` needs a Long.

forbid (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"payments___pay_endpoint",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
unless {
  principal.hasTag("agent_class") &&
  context.input has amount_cents &&
  (
    (principal.getTag("agent_class") == "research"   && context.input.amount_cents <= 50) ||
    (principal.getTag("agent_class") == "analysis"   && context.input.amount_cents <= 200) ||
    (principal.getTag("agent_class") == "operations" && context.input.amount_cents <= 1000)
  )
};


// -----------------------------------------------------------------------------
// P04 — Payee allowlist enforced at the Gateway
// -----------------------------------------------------------------------------
// The x402 Bazaar exposes on the order of ten thousand paid endpoints that an
// agent can discover and pay autonomously. Discovery is a feature; unbounded
// payee scope is not. Anything outside the allowlist is denied here, before
// the payment layer is ever reached.

forbid (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"payments___pay_endpoint",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
unless {
  context.input has payee &&
  context.input.payee in [
    "firmdata.example.com",
    "marketdata.example.com",
    "news.example.com"
  ]
};


// -----------------------------------------------------------------------------
// P05 — Subscriptions and anything with a duration require a procurement role
// -----------------------------------------------------------------------------
// This is the stateless half of the policy that would have stopped Section
// 13.1. A per-lookup micropayment is a transaction. A purchase that grants
// access over time is a commitment, it recurs, and an agent has no way to
// know whether the organization already holds one. Commitments get a human.
//
// 2nd edition: the first edition checked an approval token in
// context.input (`like "fin-approval-*"`). context.input is written by the
// model, so a format check on it is forgeable: the agent can type a string
// that matches. Chapter 3's provenance rule says conditions must read values
// the model cannot write. This version reads a claim that the identity
// provider puts on the principal, which only a human procurement session
// carries. An analysis agent's token never has it, so the agent cannot buy
// a pass however good its arithmetic is.

forbid (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"payments___purchase_access",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
unless {
  principal.hasTag("procurement_role") &&
  principal.getTag("procurement_role") == "approver"
};


// -----------------------------------------------------------------------------
// P06 — Deny larger payments outside business hours
// -----------------------------------------------------------------------------
// Micropayments run continuously; that is the point. Larger autonomous spend
// at 03:00 on a Sunday has no legitimate pattern in most organizations, and
// is the signature of both a runaway loop and a compromised session.
//
// 2nd edition: the 1st edition read context.input.request_hour, a value the
// model writes. This version reads context.system.now, the UTC timestamp
// AgentCore Policy supplies on every request (same as Chapter 3, Policy 09).
// The window 13:00-01:00 UTC is 07:00-19:00 at UTC-6; adjust to your hours.

forbid (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"payments___pay_endpoint",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
when {
  context.input has amount_cents &&
  context.input.amount_cents > 100
}
unless {
  duration("13h") <= context.system.now.toTime() ||
  context.system.now.toTime() < duration("1h")
};


// -----------------------------------------------------------------------------
// P07 — Tenant attribution is mandatory on every payment
// -----------------------------------------------------------------------------
// A payment without a tenant is a payment you cannot allocate, cannot bill
// back, and cannot explain. In a multi-tenant agent this is also the control
// that keeps one customer's workload from spending another customer's budget.

forbid (
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"payments___pay_endpoint",
    AgentCore::Action::"payments___purchase_access"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
unless {
  context.input has tenant_id &&
  principal.hasTag("tenant") &&
  principal.getTag("tenant") == context.input.tenant_id
};
