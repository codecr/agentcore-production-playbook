# Session Storage Production Adoption Checklist

This checklist accompanies Chapter 6 of *AgentCore in Production: The Operator's Playbook*. Walk through it before promoting a Session Storage-backed agent to production. Each item maps to a specific failure mode covered in the chapter.

---

## Before deployment

### Preview status
- [ ] The team has acknowledged that Session Storage is in **public preview** (since March 25, 2026). Pricing, limits, and behaviors may change before GA.
- [ ] Business-critical decisions do not assume Session Storage will remain exactly as it is today. The architecture has a documented fallback if the service becomes unsuitable for the workload.

### Regional fit
- [ ] The deployment region is one of the 14 supported preview regions (US East N. Virginia, US East Ohio, US West Oregon, Canada Central, Asia Pacific Mumbai, Seoul, Singapore, Sydney, Tokyo, Europe Frankfurt, Ireland, London, Paris, Stockholm).
- [ ] If the workload requires a region not on the list, the team has a documented plan for when that region gains support.

### Limits and capacity
- [ ] No session is expected to exceed **1 GB of state** (preview limit).
- [ ] If the workload could grow beyond 1 GB, the team has a plan for splitting state across sessions, archiving to S3, or moving the workload off Session Storage.
- [ ] The team understands the 14-day idle reset and has decided whether sessions in the workload routinely exceed that threshold.

---

## Session ID strategy

### Generation
- [ ] Session IDs are generated programmatically, not hardcoded.
- [ ] Every session ID is at least **33 characters** (validated client-side by AgentCore).
- [ ] Session IDs encode a stable user/tenant identifier so the same user resuming the same workspace gets the same filesystem.
- [ ] Session IDs are not predictable across users (otherwise a multi-tenant collision risk exists).
- [ ] The `generate_session_id()` helper from `utils/session_lifecycle.py` (or an equivalent) is used.

### Isolation verification
- [ ] A test confirms that two different users with different `runtimeSessionId` values cannot read each other's filesystem state.
- [ ] A test confirms that a user with a session in one tenant scope cannot reach files in another tenant's session.

---

## Filesystem semantics

### Compatibility checks
- [ ] Tools the agent uses are confirmed to NOT depend on:
  - Hard links (use symlinks instead)
  - Device files, FIFOs, or UNIX sockets
  - Extended attributes (xattr)
  - `fallocate` / sparse file preallocation
  - File locking that persists across stop/resume
- [ ] If any of these are required, the team has a workaround documented (e.g., avoid the affected operation, sync to S3 for state that needs locking).

### Permissions
- [ ] The team understands that file permissions are stored but not enforced (single user in the microVM). Multi-user permission models do not work.

---

## Stop/resume operations

### Safe stop
- [ ] Every explicit session stop uses the `safe_stop_session()` pattern (or equivalent): issues `StopRuntimeSession`, then waits out a fixed flush grace period before returning. There is no AgentCore API to poll for flush completion — confirm the team understands this is a documented approximation, not a guarantee, before depending on it.
- [ ] Application code does not assume a session is stopped and flushed immediately after the `stop_runtime_session` call returns. It waits at least the grace period.
- [ ] The grace period (`flush_wait_seconds`, default 15s) is sized for the workload's typical workspace size — pad it for agents that write large amounts of state before stopping.

### Resume after stop
- [ ] On session resume, the agent calls `workspace_status()` (or equivalent) before performing any work, to determine what state already exists.
- [ ] The agent's code is idempotent: calling `initialize_workspace()` on a session that has been initialized is a no-op.

### Orphan detection
- [ ] A periodic job (daily, weekly) calls `find_orphan_sessions()` against each production runtime. There is no native "list a runtime's sessions" API — this is a CloudWatch Logs Insights approximation over the runtime's own OTEL logs, grouped by session, using the most recent logged timestamp as a proxy for last-invoked. The team understands it only sees sessions that logged something in the scanned window, and depends on the runtime's automatic instrumentation populating a `session.id` field (verified against this runtime's actual log output, not assumed).
- [ ] The job's execution identity has `logs:StartQuery` and `logs:GetQueryResults` on the runtime's log group.
- [ ] Orphan sessions older than the team's threshold are either resumed and stopped properly, or accepted as cost.
- [ ] An alert fires if orphan count exceeds a configurable threshold.

---

## Deploy-time gotcha

### Runtime version updates
- [ ] The team understands: **deploying a new runtime version resets the filesystem of all active sessions**.
- [ ] The deploy strategy is documented:
  - [ ] Option A: maintenance window — pause all active sessions, deploy, expect all workspaces to reset
  - [ ] Option B: blue-green — deploy to a new runtime ARN, route new sessions to it, let old sessions drain on the old runtime
  - [ ] Option C: never update (only for static agents — usually wrong)
- [ ] If Option B, the load balancer / API Gateway / routing layer is configured to support multiple runtime ARNs simultaneously.

### Active session notifications
- [ ] Before any runtime version deploy that affects live sessions, owners of active sessions are notified.
- [ ] The notification mechanism is automated (not "the team will remember").

---

## Cost controls

### Storage cost monitoring
- [ ] Session count is tracked as a CloudWatch metric.
- [ ] Average session size (GB) is tracked or estimated.
- [ ] An alert fires if total Session Storage volume exceeds expected limits.

### Session lifecycle policies
- [ ] The application layer enforces an explicit session timeout (in addition to AWS's 14-day idle reset). Recommended: 24h to 7d depending on workload.
- [ ] After the application timeout, sessions are explicitly stopped via the safe-stop pattern.
- [ ] Long-running sessions (> 4 hours of active runtime) are reviewed weekly for whether they should be split into shorter sessions.

---

## Security

### IAM scoping
- [ ] The runtime's execution role grants only the permissions the agent actually needs.
- [ ] The role does NOT have `bedrock-agentcore-control:*` permissions (it should not be able to stop other sessions, modify the runtime, or list other tenants' resources).
- [ ] If shell commands are enabled, the role has the absolute minimum filesystem and network permissions required.

### Cedar policies
- [ ] AgentCore Policy rules (Chapter 3) gate which tool calls the agent can make.
- [ ] If `InvokeAgentRuntimeCommand` is exposed, a Cedar policy restricts which commands can be executed.

### Observability
- [ ] Every session invocation generates a CloudWatch Transaction Search trace.
- [ ] Session stop events are logged with the session ID, the stopper identity, and the flush confirmation status.
- [ ] Resume events are logged with what state existed at resume time (file count, total size, last commit hash in Git, etc.).

---

## Workload-specific verifications (Acme SaaS runbook agent example)

The exact checks depend on the workload. For the runbook generation agent in this chapter, the workload-specific items would include:

- [ ] Git repository in the workspace has a `.gitignore` that excludes large artifacts (node_modules, build outputs).
- [ ] The agent commits at every meaningful checkpoint (after each runbook is written), not just at session end.
- [ ] Runbooks include the analyzed ticket IDs as a footer, so the audit trail is preserved even if the workspace is reset.
- [ ] The `.tickets_analyzed.txt` log is committed to Git, not just left as an untracked file.

Adapt this section to the specific workload before going to production.

---

## Sign-off

- [ ] All items above are checked or have documented exceptions.
- [ ] The team has run at least one end-to-end stop/resume test in the production environment.
- [ ] The team has run at least one runtime version update test and confirmed the blue-green or maintenance-window strategy works.
- [ ] An on-call runbook exists for: orphan session cleanup, runtime version deploy, session ID collision investigation, workspace recovery from a different session.

---

*This checklist is updated as the AgentCore Session Storage service evolves. Verify against the AWS documentation for current behaviors.*
