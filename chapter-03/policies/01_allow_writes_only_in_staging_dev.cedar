// Policy: 01_allow_writes_only_in_staging_dev
// Category: Environment boundaries
// Intent: Permit mutating tools only on the staging and dev Gateway Targets.
//         There is no permit for the devops-prod target, so production is
//         protected by default-deny.
//
// Trusted input: the ACTION NAME. The environment is encoded in the Target
//   (devops-dev, devops-staging, devops-prod), and each Target's Lambda runs
//   with credentials that can only reach its own environment. The model can
//   pick which tool to call, but it cannot make the staging tool touch
//   production. 2nd edition change: the 1st edition tested
//   context.input.environment, a value the model writes. An agent could send
//   environment="staging" with a production instance ID and pass the check.
// Protects against: The eager interpreter archetype (Chapter 1, Section 1.5).
//                   The 2:37 AM incident where an agent restarts production.

permit (
  principal,
  action in [
    AgentCore::Action::"devops-dev___restart_service",
    AgentCore::Action::"devops-dev___apply_config",
    AgentCore::Action::"devops-dev___scale_deployment",
    AgentCore::Action::"devops-staging___restart_service",
    AgentCore::Action::"devops-staging___apply_config",
    AgentCore::Action::"devops-staging___scale_deployment"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
);
