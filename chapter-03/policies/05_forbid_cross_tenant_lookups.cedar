// Policy: 05_forbid_cross_tenant_lookups
// Category: Tenant isolation
// Intent: An explicit forbid that fires whenever a request would return data
//         tagged with a tenant different from the principal's tenant. This
//         is the belt-and-suspenders companion to policy 04: even if the
//         tenant_id was somehow omitted from the request context, this rule
//         denies the call rather than letting default-permit fall through.
//
//         The `unless` clause carves out the one intentional exception:
//         policy 06's tenant-admin audit override. Cedar's forbid-overrides-
//         permit model means a bare forbid here would always win, so this
//         forbid must mirror policy 06's permit condition exactly (tenant-
//         scoped admin + a ticket-shaped context.input.audit_reason) or 06
//         can never fire. Keep the two clauses in sync if either changes.
// Protects against: Subtle bugs in upstream tenant resolution. Mistakes in
//                   policy 04's permits being too broad.

forbid (
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
)
when {
  principal.hasTag("tenant") &&
  context.input has tenant_id &&
  principal.getTag("tenant") != context.input.tenant_id
}
unless {
  principal.hasTag("role") &&
  principal.getTag("role") == "tenant_admin" &&
  context.input has audit_reason &&
  context.input.audit_reason like "ticket-*"
};
