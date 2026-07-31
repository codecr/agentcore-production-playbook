// Policy: 15_explicit_forbid_for_known_bad_patterns
// Category: Defense in depth
// Intent: A catch-all forbid for patterns observed in past incidents. This
//         policy is meant to grow over time — every production incident
//         that surfaces a new failure pattern should add a clause here.
//         The current rules block:
//         - Drop/delete operations on tables matching "audit_*" or "*_log*"
//         - Service restarts where the service name contains "payment" or "billing"
//           outside of pre-approved maintenance windows
//
//         This policy embodies the "wisdom of incidents" — the lessons
//         from things that have gone wrong, encoded in code.

forbid (
  principal,
  action == AgentCore::Action::"data___drop_table",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has table_name &&
  (context.input.table_name like "audit_*" || context.input.table_name like "*_log*")
};
