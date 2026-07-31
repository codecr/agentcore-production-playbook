// Policy: 13_rate_limit_destructive_actions
// Category: Defense in depth
// Intent: Leverage context attributes set by an upstream rate limiter to
//         block destructive tool calls when the principal has exceeded a
//         threshold. The rate limiter (typically API Gateway or a Lambda
//         authorizer) sets context.input.destructive_count_last_hour; this
//         policy denies destructive tools when the count exceeds 5.
//
//         NOTE: This is a `forbid` rule, not a permit. It does not grant
//         access by itself; it denies when the rate is exceeded.

forbid (
  principal,
  action in [
    AgentCore::Action::"devops___terminate_instance",
    AgentCore::Action::"devops___delete_resource"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has destructive_count_last_hour &&
  context.input.destructive_count_last_hour > 5
};
