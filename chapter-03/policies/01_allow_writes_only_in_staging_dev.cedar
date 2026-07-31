// Policy: 01_allow_writes_only_in_staging_dev
// Category: Environment boundaries
// Intent: Permit mutating tool calls only when the request explicitly targets
//         a non-production environment. Default-deny protects production.
// Protects against: The eager interpreter archetype (Chapter 1, Section 1.5).
//                   The 2:37 AM incident where an agent restarts production.
//
// NOTE: Replace the resource ARN with your Gateway ARN at deploy time.

permit (
  principal,
  action in [
    AgentCore::Action::"devops___restart_service",
    AgentCore::Action::"devops___apply_config",
    AgentCore::Action::"devops___scale_deployment"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has environment &&
  (context.input.environment == "staging" || context.input.environment == "dev")
};
