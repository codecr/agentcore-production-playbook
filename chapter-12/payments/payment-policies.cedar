// =============================================================================
// payment-policies.cedar — Cedar policies for payment-capable tools
// =============================================================================
// Accompanies Chapter 12 of "AgentCore in Production: The Operator's Playbook".
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
// None of the three substitutes for the others. The incident in Section 12.1
// passed the first, was never subjected to the second, and had no third.
//
// Deploy with the same discipline as Chapter 3: LOG_ONLY first, read the
// decision logs, then ENFORCE.
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
  resource
)
unless {
  principal.hasTag("session_environment") &&
  principal.getTag("session_environment") == "production"
};


// -----------------------------------------------------------------------------
// P03 — Per-call amount ceiling by role, at the Gateway
// -----------------------------------------------------------------------------
// The same tiered pattern as the refund policy in Chapter 3, applied to
// autonomous spend. Note the ceilings are deliberately low: these are
// micropayments, and any single call asking for more than a couple of
// dollars is either a mistake or a subscription, and both deserve a human.
//
// Unlike Chapter 3's whole-dollar refund caps, these ceilings are
// sub-dollar and need fractional precision, so amount_usd is a Cedar
// `decimal`, not a `Long`. Cedar has no bare decimal literal and its
// native `<=`/`>` operators only accept `long`/`datetime`/`duration` — a
// decimal comparison has to go through the extension methods
// (`lessThanOrEqual`, `greaterThan`, ...) against a `decimal("...")`
// value. The Gateway must submit amount_usd in the request context as a
// Cedar decimal, using Cedar's JSON encoding for extension values:
//
//     "amount_usd": {"__extn": {"fn": "decimal", "arg": "0.50"}}
//
// The `__extn` wrapper is required. Sending a bare JSON number (0.50), a
// string ("0.50"), or the inner object without the wrapper
// ({"fn": "decimal", "arg": "0.50"}) all fail the same way: Cedar raises
// `type error: expected decimal, got <record|string>` while evaluating
// this policy, the policy does not satisfy, and the request falls through
// to default-deny. That failure is silent from the caller's point of view
// — a well-formed, in-budget payment simply gets denied with no
// indication that the context encoding was the problem. Verified with
// cedarpy against this policy; see the ch12 decimal evaluation checks in
// VALIDATION.md.

permit (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"payments___pay_endpoint",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/acme-lending"
)
when {
  principal.hasTag("agent_class") &&
  context.input has amount_usd &&
  (
    (principal.getTag("agent_class") == "research"   && context.input.amount_usd.lessThanOrEqual(decimal("0.50"))) ||
    (principal.getTag("agent_class") == "analysis"   && context.input.amount_usd.lessThanOrEqual(decimal("2.00"))) ||
    (principal.getTag("agent_class") == "operations" && context.input.amount_usd.lessThanOrEqual(decimal("10.00")))
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
  principal,
  action == AgentCore::Action::"payments___pay_endpoint",
  resource
)
unless {
  context.input has payee &&
  context.input.payee in [
    "bureau.example.com",
    "marketdata.example.com",
    "news.example.com"
  ]
};


// -----------------------------------------------------------------------------
// P05 — Subscriptions and anything with a duration require human approval
// -----------------------------------------------------------------------------
// This is the policy that would have stopped Section 12.1 outright. A
// per-query micropayment is a transaction. A purchase that grants access
// over time is a commitment, it recurs, and an agent has no way to know
// whether the organization already holds one. Commitments get a human.
//
// The approval token comes from outside the agent — a ticket, an approval
// bot, a finance workflow — exactly as in Chapter 3's Policy 02.

forbid (
  principal,
  action == AgentCore::Action::"payments___purchase_access",
  resource
)
unless {
  context.input has approval_token &&
  context.input.approval_token like "fin-approval-*"
};


// -----------------------------------------------------------------------------
// P06 — Deny payments outside business hours for high-value classes
// -----------------------------------------------------------------------------
// Micropayments run continuously; that is the point. Larger autonomous spend
// at 03:00 on a Sunday has no legitimate pattern in most organizations, and
// is the signature of both a runaway loop and a compromised session. The hour
// is supplied by the Runtime from a trusted clock, not by the agent.
//
// amount_usd is a Cedar decimal here too (see P03) — the comparison goes
// through `.greaterThan(decimal("1.00"))` rather than the native `>`.

forbid (
  principal,
  action == AgentCore::Action::"payments___pay_endpoint",
  resource
)
when {
  context.input has amount_usd &&
  context.input.amount_usd.greaterThan(decimal("1.00")) &&
  context.input has request_hour &&
  (context.input.request_hour < 7 || context.input.request_hour > 19)
};


// -----------------------------------------------------------------------------
// P07 — Tenant attribution is mandatory on every payment
// -----------------------------------------------------------------------------
// A payment without a tenant is a payment you cannot allocate, cannot bill
// back, and cannot explain. In a multi-tenant agent this is also the control
// that keeps one customer's workload from spending another customer's budget.

forbid (
  principal,
  action in [
    AgentCore::Action::"payments___pay_endpoint",
    AgentCore::Action::"payments___purchase_access"
  ],
  resource
)
unless {
  context.input has tenant_id &&
  principal.hasTag("tenant") &&
  principal.getTag("tenant") == context.input.tenant_id
};
