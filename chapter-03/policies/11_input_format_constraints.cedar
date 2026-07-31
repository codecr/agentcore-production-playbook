// Policy: 11_input_format_constraints
// Category: Input validation
// Intent: Enforce format constraints on string inputs using Cedar's `like`
//         pattern matching. This example validates that ticket_id follows
//         the ticket-<anything> format before a ticket update is permitted.
//         The pattern extends to other string-shaped inputs (user IDs as
//         UUIDs, etc.) the same way — add a clause per field. This is not a
//         substitute for tool-level validation, but it catches obvious
//         malformed inputs early.

permit (
  principal,
  action == AgentCore::Action::"support___update_ticket",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has ticket_id &&
  context.input.ticket_id like "ticket-*"
};
