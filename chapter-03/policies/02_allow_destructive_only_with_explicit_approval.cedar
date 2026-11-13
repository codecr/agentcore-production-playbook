// Policy: 02_allow_destructive_only_with_explicit_approval
// Category: Environment boundaries
// Intent: Destructive actions require a human approval that was verified
//         OUTSIDE the model, before Cedar runs.
//
// Trusted input: context.input.approval_verified, written by the Gateway
//   REQUEST interceptor (interceptor/trusted_context_interceptor.py). The
//   agent passes only approval_id, a lookup key. The interceptor asks the
//   approval service whether that approval exists, was granted by a human
//   other than the requester, covers this exact action and resource, has not
//   expired, and has not been used. Only then does it write
//   approval_verified=true. Any approval_verified value the model supplied
//   is rejected before Cedar sees the request.
//   The target Lambda re-checks the approval with the same service
//   (defense in depth): a policy is not the only gate.
//
// 2nd edition change: the 1st edition checked
//   context.input.approval_token like "approved-*". The model can write any
//   string that matches a pattern, so that check proved nothing.
// See also: policies/temporal/17_single_use_approval.dogwood for a Dogwood
//   variant that reads the approval from a tool response instead.

permit (
  principal,
  action in [
    AgentCore::Action::"devops-prod___terminate_instance",
    AgentCore::Action::"devops-prod___delete_resource",
    AgentCore::Action::"data___drop_table"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  context.input has approval_verified &&
  context.input.approval_verified == true
};
