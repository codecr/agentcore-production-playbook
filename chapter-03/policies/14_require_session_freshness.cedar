// Policy: 14_require_session_freshness
// Category: Defense in depth
// Intent: Refuse high-risk tools when the user authenticated more than an
//         hour ago. Forces re-authentication for sensitive operations.
//
// Trusted input: context.input.auth_age_seconds, written by the Gateway
//   REQUEST interceptor from the JWT's auth_time claim (falling back to iat).
//   Policy cannot compute it: AgentCore exposes every JWT claim as a STRING
//   tag, and Cedar cannot do arithmetic on strings. A value the model
//   supplied is rejected by the interceptor.
// 2nd edition changes: (1) the 1st edition said AgentCore Identity computes
//   session_age_seconds; it does not. (2) Now a forbid; as a permit it
//   granted refunds of any amount to anyone with a fresh token.

forbid (
  principal,
  action in [
    AgentCore::Action::"billing___process_refund",
    AgentCore::Action::"data___export_user_data"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
unless {
  context.input has auth_age_seconds &&
  context.input.auth_age_seconds < 3600
};
