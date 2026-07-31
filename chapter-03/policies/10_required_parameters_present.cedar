// Policy: 10_required_parameters_present
// Category: Input validation
// Intent: Enforce that critical parameters are present in the request. Many
//         agent failures involve calling tools with missing arguments; Policy
//         catches these at the Gateway boundary rather than letting them
//         become Lambda 500s. Example: the support ticket creation tool
//         requires both tenant_id and priority.

permit (
  principal,
  action == AgentCore::Action::"support___create_ticket",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has tenant_id &&
  context.input has priority &&
  context.input has title
};
