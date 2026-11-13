// Policy: 12_numeric_range_constraints
// Category: Input validation
// Intent: Sanity bounds on refund amounts regardless of role: never below 1,
//         never above 50,000.
// 2nd edition change: now a forbid. As a permit (1st edition), it granted
//   any caller refunds up to 50,000, which silently overrode the role caps
//   in policy 08: permits add up, so the most generous one wins. A sanity
//   bound has to subtract, and only forbid subtracts.

forbid (
  principal,
  action == AgentCore::Action::"billing___process_refund",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
unless {
  context.input has amount &&
  context.input.amount >= 1 &&
  context.input.amount <= 50000
};
