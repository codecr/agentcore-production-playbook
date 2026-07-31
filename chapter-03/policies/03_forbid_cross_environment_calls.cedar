// Policy: 03_forbid_cross_environment_calls
// Category: Environment boundaries
// Intent: An explicit forbid that catches the case where the agent's workload
//         identity is bound to one environment but the request targets a
//         different environment. This is a forbid-overrides-permit guard:
//         even if some other policy would permit, this denies first.
// Protects against: Agent sessions that drift across environments via long
//                   conversations. Defense in depth against policies 01 and 02
//                   being insufficient.

forbid (
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("session_environment") &&
  context.input has environment &&
  principal.getTag("session_environment") != context.input.environment
};
