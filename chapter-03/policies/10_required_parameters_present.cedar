// Policy: 10_required_parameters_present
// Category: Input validation
// Intent: Refuse ticket creation unless the critical parameters are present.
//         Catches malformed calls at the Gateway rather than as Lambda 500s.
// 2nd edition change: now a forbid. As a permit it granted create_ticket to
//   every caller who supplied the three fields. Who may create tickets is a
//   separate grant (your role permits); this policy only constrains shape.

forbid (
  principal,
  action == AgentCore::Action::"support___create_ticket",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
unless {
  context.input has tenant_id &&
  context.input has priority &&
  context.input has title
};
