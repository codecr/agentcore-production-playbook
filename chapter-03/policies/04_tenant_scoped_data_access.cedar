// Policy: 04_tenant_scoped_data_access
// Category: Tenant isolation
// Intent: The canonical Acme SaaS policy. Permit tool calls only when the
//         tenant in the request matches the tenant in the principal's JWT.
//         This is the policy that would have prevented the cross-tenant
//         invoice leak from Chapter 2.
// Protects against: The identity confusion archetype (Chapter 1, Section 1.5).
//                   Cross-tenant data leakage in multi-tenant SaaS agents.

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
  principal.hasTag("tenant") &&
  context.input has tenant_id &&
  principal.getTag("tenant") == context.input.tenant_id
};
