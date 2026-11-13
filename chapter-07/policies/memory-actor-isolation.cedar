// Memory fine-grained access control (FGAC), Chapter 7 Section 7.8.
// Requires: a Gateway with an AgentCore Memory connector target named
// "AcmeMemory", and a policy engine attached to that Gateway.
// A caller may create and list its own events only: the request's actorId
// must equal the JWT "sub" claim. Start in LOG_ONLY.
// Each action listed here carries actorId in its request (body for
// CreateEvent, path for ListEvents). A policy that reads a field an
// operation does not carry still reaches ACTIVE and then denies with 403.
// STATUS: not evaluated against a live policy engine.
permit (
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"AcmeMemory___POST:/memories/{memoryId}/events",
    AgentCore::Action::"AcmeMemory___POST:/memories/{memoryId}/actor/{actorId}/sessions/{sessionId}"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"
)
when {
  principal.hasTag("sub") &&
  context has input && context.input has actorId &&
  context.input.actorId == principal.getTag("sub")
};
