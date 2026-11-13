// Policy: 06_admin_tenant_override_with_audit
// Category: Tenant isolation
// Intent: Let tenant admins read another tenant's records only when an open
//         support ticket, verified outside the model, covers that tenant.
//
// Trusted input: context.input.ticket_tenant. The agent passes ticket_id (a
//   lookup key). The Gateway REQUEST interceptor asks the ticketing system
//   whether the ticket exists, is open, is assigned to this caller (JWT sub),
//   and which tenant it concerns, then writes that tenant into
//   ticket_tenant. A ticket_tenant supplied by the model is rejected.
//   Every override is therefore traceable to a real ticket, and the
//   interceptor logs ticket_id next to the Policy decision.
//
// 2nd edition change: the 1st edition accepted any audit_reason matching
//   "ticket-*". A regex match is not an audit; the model can write
//   "ticket-1" as easily as anything else.
// Scope: read tools only. Writes across tenants are never overridden here.

permit (
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"billing___get_invoice_details",
    AgentCore::Action::"billing___list_invoices",
    AgentCore::Action::"support___get_ticket"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "tenant_admin" &&
  context.input has tenant_id &&
  context.input has ticket_tenant &&
  context.input.ticket_tenant == context.input.tenant_id
};
