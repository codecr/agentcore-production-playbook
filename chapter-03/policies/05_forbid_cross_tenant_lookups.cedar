// Policy: 05_forbid_cross_tenant_lookups
// Category: Tenant isolation
// Intent: An explicit forbid that fires whenever the tenant in the request
//         differs from the tenant on the caller's JWT. Belt-and-suspenders
//         companion to policy 04.
//
//         The unless clause carves out the one intentional exception, policy
//         06's verified tenant-admin override. Because forbid overrides
//         permit, this clause must mirror policy 06 exactly or 06 can never
//         fire. tests/test_tenant_isolation.py keeps the two in sync.
//
// Trusted inputs: principal tags "tenant" and "role" (JWT claims), and
//   context.input.ticket_tenant, written by the REQUEST interceptor after it
//   verified the ticket with the ticketing system (see policy 06).
// Note: the action is unconstrained, but the resource must still be the
//   specific Gateway ARN (live-tested 2026-11-10). The type test
//   "resource is AgentCore::Gateway" makes validation fail with
//   `unrecognized action` for every action named in the unless clause.
//   The `context has input` guard is required too: with an open action the
//   validator also checks InvokeLLM and Http requests, which carry no input.
// Protects against: Subtle bugs in upstream tenant resolution. Mistakes in
//                   policy 04's permits being too broad.

forbid (
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("tenant") &&
  context has input &&
  context.input has tenant_id &&
  principal.getTag("tenant") != context.input.tenant_id
}
unless {
  principal.hasTag("role") &&
  principal.getTag("role") == "tenant_admin" &&
  action in [
    AgentCore::Action::"billing___get_invoice_details",
    AgentCore::Action::"billing___list_invoices",
    AgentCore::Action::"support___get_ticket"
  ] &&
  context.input has ticket_tenant &&
  context.input.ticket_tenant == context.input.tenant_id
};
