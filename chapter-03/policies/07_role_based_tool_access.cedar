// Policy: 07_role_based_tool_access
// Category: Role-based access
// Intent: Restrict tool access by role claim on the principal's JWT. The
//         markdown_to_email tool, for example, can only be used by callers
//         whose token carries a scope containing "manager". Patterned on
//         AWS's official example from the AgentCore samples.
// Protects against: Users invoking tools they should not have access to,
//                   even if their identity is otherwise valid.

permit (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"comms___markdown_to_email",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("scope") &&
  principal.getTag("scope") like "*manager*"
};
