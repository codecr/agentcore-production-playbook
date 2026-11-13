// Policy: 11_input_format_constraints
// Category: Input validation
// Intent: Refuse ticket updates whose ticket_id does not follow the
//         ticket-<anything> format. This constrains a model-written value,
//         which is safe: constraining what the model wrote never trusts it.
//         Not a substitute for tool-level validation.
// 2nd edition change: now a forbid (see policy 10 for why).

forbid (
  principal,
  action == AgentCore::Action::"support___update_ticket",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
unless {
  context.input has ticket_id &&
  context.input.ticket_id like "ticket-*"
};
