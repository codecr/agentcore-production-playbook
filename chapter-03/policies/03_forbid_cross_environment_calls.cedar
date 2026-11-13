// Policy: 03_forbid_cross_environment_calls
// Category: Environment boundaries
// Intent: A session bound to staging or dev can never reach a production
//         Target, whatever other policies permit (forbid overrides permit).
//
// Trusted input: the principal's "env" tag, i.e. the "env" claim on the JWT
//   that authenticated the caller. Your identity provider mints that claim
//   (for example a Cognito pre-token-generation trigger that binds the
//   session to an environment); the Gateway's JWT authorizer validates the
//   signature and AgentCore Policy exposes every claim as a string tag.
//   AgentCore Identity does not invent claims; it validates them.
//   2nd edition change: the 1st edition compared that claim with
//   context.input.environment, a model-written value.
// IAM-authenticated callers (AgentCore::IamEntity) carry no claims; they
//   reach production only if their role name matches the ProdOps* family.
// Note: a missing claim also denies (unless requires the claim to be "prod").

forbid (
  principal,
  action in [
    AgentCore::Action::"devops-prod___restart_service",
    AgentCore::Action::"devops-prod___apply_config",
    AgentCore::Action::"devops-prod___scale_deployment",
    AgentCore::Action::"devops-prod___terminate_instance",
    AgentCore::Action::"devops-prod___delete_resource",
    AgentCore::Action::"devops-prod___deploy"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
unless {
  (principal is AgentCore::OAuthUser &&
   principal.hasTag("env") &&
   principal.getTag("env") == "prod") ||
  (principal is AgentCore::IamEntity &&
   principal.id like "arn:aws:sts::*:assumed-role/ProdOps*")
};
