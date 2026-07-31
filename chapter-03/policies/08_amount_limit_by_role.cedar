// Policy: 08_amount_limit_by_role
// Category: Role-based access
// Intent: Refund tool with role-tiered amount caps. Support agents can refund
//         up to $200; supervisors up to $1000; managers up to $5000. The
//         policy expresses this as three separate permit rules with mutually
//         exclusive role conditions, evaluated together by Cedar.
// Protects against: Privilege escalation through tool calls. An agent acting
//                   on behalf of a low-tier user cannot exceed that user's
//                   actual authority.

permit (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"billing___process_refund",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("role") &&
  context.input has amount &&
  (
    (principal.getTag("role") == "support_agent" && context.input.amount <= 200) ||
    (principal.getTag("role") == "supervisor"    && context.input.amount <= 1000) ||
    (principal.getTag("role") == "manager"       && context.input.amount <= 5000)
  )
};
