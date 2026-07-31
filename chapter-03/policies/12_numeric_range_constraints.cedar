// Policy: 12_numeric_range_constraints
// Category: Input validation
// Intent: Bound numeric inputs to sane ranges. Refund amounts cannot be
//         negative or above $50,000 regardless of role. Search result limits
//         cannot exceed 1000. These are sanity bounds enforced at the policy
//         layer in addition to whatever the tool itself validates.

permit (
  principal,
  action == AgentCore::Action::"billing___process_refund",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has amount &&
  context.input.amount > 0 &&
  context.input.amount <= 50000
};
