// Policy: 09_time_of_day_restrictions
// Category: Role-based access
// Intent: High-risk tools (deployments, schema changes) only during business
//         hours. The hour comes from context.input.request_hour, set by the
//         caller (the agent's Runtime) from a trusted clock source.
//
//         IMPORTANT: Cedar does not have built-in time semantics. The
//         context.input.request_hour must be populated by the calling
//         system; do not allow the model to set this value. The Runtime
//         injects it from server time before invoking the Gateway.

permit (
  principal,
  action in [
    AgentCore::Action::"devops___deploy",
    AgentCore::Action::"data___apply_schema_change"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has request_hour &&
  context.input.request_hour >= 9 &&
  context.input.request_hour < 18
};
