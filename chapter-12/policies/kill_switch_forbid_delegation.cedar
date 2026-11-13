// Chapter 12, Section 12.10 — kill switch, narrowest form.
//
// Stateless on purpose: creating or deleting a stateless Cedar policy did not
// invalidate live policy sessions in the author's October 2026 lab, while any
// create/update/delete of a TEMPORAL policy invalidated them (-32005).
// Action id live-tested 2026-11-11 (CT-12-03): <target>___POST:/, with a hyphenated target name.

forbid (
  principal,
  action == AgentCore::Action::"refunds-agent___POST:/",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"
);
