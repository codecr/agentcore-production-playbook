// Policy: 14_require_session_freshness
// Category: Defense in depth
// Intent: For high-risk tools, require that the session is "fresh" — within
//         the first hour since user authentication. Stale sessions, where
//         the user authenticated a long time ago and the agent is still
//         operating, are denied. Forces re-authentication for sensitive
//         operations.
//
//         The session_age_seconds is set by AgentCore Identity from the
//         JWT's iat (issued-at) claim; the agent cannot fake it.

permit (
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"billing___process_refund",
    AgentCore::Action::"data___export_user_data"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has session_age_seconds &&
  context.input.session_age_seconds < 3600
};
