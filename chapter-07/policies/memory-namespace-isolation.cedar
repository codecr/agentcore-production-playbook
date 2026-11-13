// Memory FGAC, Chapter 7 Section 7.8: long-term retrieval pinned to the
// namespace carried in a token claim.
// The identity provider issues a custom claim "memory_ns" holding the
// caller's full per-user namespace path, e.g.
//   /tenant/globex-corp/actor/<sub>/
// and the agent retrieves with namespacePath equal to that value.
// Cedar cannot concatenate strings, so the claim must carry the whole path.
// Tenant-wide namespaces (/tenant/<t>/facts/) are read by the backend with
// its own IAM role, not through this OAuth path.
// STATUS: not evaluated against a live policy engine.
permit (
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"AcmeMemory___POST:/memories/{memoryId}/retrieve",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"
)
when {
  principal.hasTag("memory_ns") &&
  context has input && context.input has namespacePath &&
  context.input.namespacePath == principal.getTag("memory_ns")
};
