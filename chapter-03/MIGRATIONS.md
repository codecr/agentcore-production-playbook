# Migrations

Two upgrade paths are covered here: from the four-policy DevOps example in
the January 2026 gerardo.dev post ("AgentCore Policy: Stop Your DevOps Agent
from Deleting Production") to this library, and from the first-edition
library (May 2026) to the second-edition library (October 2026).

## From the first-edition library (May 2026) to the second edition

The second edition fixes policies that trusted values the model writes, or
values nothing in the architecture produced, and policies that were written
as permits although they were meant to restrict. Chapter 3, Section 3.5
explains the reasoning.

| Policy | First edition | Second edition | What you must change |
|---|---|---|---|
| 01 | permit when `context.input.environment` is staging/dev | permit on `devops-dev` / `devops-staging` Targets only | Split your DevOps Target per environment; give each Target's Lambda a role that reaches only its environment |
| 02 | `approval_token like "approved-*"` | `approval_verified == true`, written by the REQUEST interceptor | Deploy `interceptor/`, wire it to your approval service, declare `approval_id` and `approval_verified` in the tool schema, re-verify in the Lambda |
| 03 | claim `session_environment` vs `context.input.environment` | forbid prod Targets unless claim `env` == "prod" (or ProdOps* IAM role) | Have your IdP mint an `env` claim |
| 05/06 | `audit_reason like "ticket-*"` | `ticket_tenant` written by the interceptor after a ticketing-system lookup; override limited to read tools; 05 uses `resource is AgentCore::Gateway` | Wire the ticket lookup; declare `ticket_id` and `ticket_tenant` in the read tools' schemas |
| 09 | permit when `context.input.request_hour` in 9..17 | forbid unless `context.system.now.toTime()` in 15h..23h UTC | Set the window in UTC |
| 10, 11, 12 | permits | `forbid ... unless` | Make sure some other permit grants the action (role permits); these only constrain |
| 13 | forbid when `context.input.destructive_count_last_hour > 5` | Dogwood temporal `count` (`policies/temporal/`) | Send `x-amzn-bedrock-agentcore-policy-session-id`, chosen by your backend; grant `bedrock-agentcore:GetWorkloadAccessToken` to the Gateway role |
| 14 | permit when `context.input.session_age_seconds < 3600` | forbid unless `auth_age_seconds < 3600`, written by the interceptor from `auth_time`/`iat` | Deploy the interceptor with `passRequestHeaders: true` |
| 16, 17 | (new) | Dogwood sequencing and single-use approval | Serialize dependent calls in your client |

Deployment order on a live engine: create permits before the forbids that
constrain them (strict validation may reject a forbid with no matching
permit as "Overly Restrictive"), or build the new set in a fresh engine and
switch the Gateway association. Adding or changing any temporal policy
invalidates live policy sessions.

## From the four-policy DevOps example (January 2026 post)

The post's four permits, and where each lands here:

1. Restart in staging/dev (`context.input.environment`): becomes policy 01.
   The post's version has the same flaw as the first-edition library; the
   environment must come from the Target, not from a tool argument. Add
   policy 03 to bind sessions to an environment.
2. Terminate in staging/dev: becomes policy 01 for non-production Targets
   plus policy 02 (verified approval) or 17 (Dogwood single-use approval)
   for production, with policy 13 as a per-session rate limit.
3. `clean_logs` with a required `log_group`: keep a role-based permit for
   the tool, and add the required-parameter check as a forbid (pattern of
   policy 10) plus an audit-log protection clause in policy 15.
4. `get_metrics` always: unchanged in spirit; a read-only grant is a permit.

Then add the tenant-isolation policies (04 to 06) if your agent is
multi-tenant, and run everything in per-policy `LOG_ONLY` first.
