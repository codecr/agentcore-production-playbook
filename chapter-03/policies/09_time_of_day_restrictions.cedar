// Policy: 09_time_of_day_restrictions
// Category: Role-based access
// Intent: High-risk tools (production deploys, schema changes) are refused
//         outside business hours.
//
// Trusted input: context.system.now, the UTC timestamp AgentCore Policy
//   supplies on every request. The model cannot write it. All times are UTC;
//   the engine does not convert time zones. 15h-23h UTC is 09:00-17:00 at
//   UTC-6; adjust the window to your own business hours.
// 2nd edition changes: (1) the 1st edition read context.input.request_hour,
//   a value somebody had to inject; context.system.now removes that
//   plumbing. (2) This is now a forbid. As a permit it GRANTED deploys to any
//   caller during business hours, adding access instead of restricting it.
//   Grants are permits; constraints are forbids.

forbid (
  principal,
  action in [
    AgentCore::Action::"devops-prod___deploy",
    AgentCore::Action::"data___apply_schema_change"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
unless {
  duration("15h") <= context.system.now.toTime() &&
  context.system.now.toTime() < duration("23h")
};
