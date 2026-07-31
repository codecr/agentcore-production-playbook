// Policy: 06_admin_tenant_override_with_audit
// Category: Tenant isolation
// Intent: Allow tenant-scoped admin users to cross tenant boundaries only
//         when an audit_reason is present in the context. This makes
//         admin-level cross-tenant access auditable: every such call is
//         tied to a documented reason that flows into observability.
// Protects against: Insider access without a paper trail. Operational
//                   queries that need cross-tenant data without compromising
//                   audit requirements.

permit (
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "tenant_admin" &&
  context.input has audit_reason &&
  context.input.audit_reason like "ticket-*"
};
