// Chapter 12, Section 12.6 — credits require a user principal two hops away.
//
// Failure prevented: "The approval nobody gave" (Section 12.1). The refunds
// Gateway saw the orchestrator's service identity and a $5,000 service permit.
//
// Provenance: `user_role` and `tenant` must be claims your IdP maps from the
// USER onto the on-behalf-of token (AgentCore Identity OAuth credential
// provider, TOKEN_EXCHANGE or JWT_AUTHORIZATION_GRANT). context.input values
// are written by a model and are only ever compared against principal tags.
//
// Template: replace the Gateway ARN and the target/tool name. Deploy LOG_ONLY
// first and confirm the action id in the decision log.
//
// Live-tested 2026-11-10 (CT-12-05) on a CUSTOM_JWT Gateway: both statements reach
// ACTIVE under FAIL_ON_ANY_FINDINGS in this order. The permit must guard every
// getTag with hasTag, and the forbid must be scoped to AgentCore::OAuthUser,
// or it is reported as Overly Restrictive for AgentCore::IamEntity.

permit (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"refunds___issue_credit",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"
)
when {
  principal.hasTag("tenant") && principal.hasTag("user_role") &&
  context.input has tenant_id && context.input has amount &&
  principal.getTag("tenant") == context.input.tenant_id &&
  ((principal.getTag("user_role") == "billing_admin" && context.input.amount <= 500) ||
   (principal.getTag("user_role") == "account_manager" && context.input.amount <= 5000))
};

forbid (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"refunds___issue_credit",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"
)
unless { principal.hasTag("user_role") && principal.hasTag("tenant") };
