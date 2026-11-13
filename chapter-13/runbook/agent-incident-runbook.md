# Agent Incident Runbook

Accompanies Chapter 13 of *AgentCore in Production: The Operator's Playbook*.

A template for the runbook every production agent needs. Copy it per agent
and fill in the specifics — a generic runbook is a document, a specific one
is a tool.

The organizing idea: **agent incidents mostly do not announce themselves.**
Traditional incidents arrive as errors, timeouts, and alarms. Agent incidents
arrive as a customer complaint, a finance question, or a quality metric that
drifted for nine days. The triage steps below are ordered accordingly.

---

## Agent identity

| Field | Value |
|---|---|
| Agent name | `acme-support-agent` |
| Runtime ARN | `arn:aws:bedrock-agentcore:us-east-1:...:runtime/...` |
| Owner | Squad Support Platform |
| Primary contact | #acme-support-platform |
| Escalation | platform-oncall (PagerDuty) |
| Registry record | `acme-support-agent` in `acme-agent-catalog` |
| Gateway | `acme-production-gateway` |
| Model | configured at `TRIAGE_MODEL_ID`; current value in runtime config |
| Evaluation config | `acme-support-online-eval` |
| Dashboards | `agent-quality-acme-support`, `kb-health-acme-docs` |

---

## Severity guide

| Sev | Definition | Examples |
|---|---|---|
| SEV1 | Customer data exposure, unauthorized action taken, or money moved incorrectly | Cross-tenant leak, refund issued without authorization, runaway spend |
| SEV2 | Agent unavailable or systematically wrong for a class of requests | Tool selection collapsed after a deploy, KB missing critical documents |
| SEV3 | Quality degraded but the agent is functioning | Helpfulness down, more escalations than usual |
| SEV4 | Single-session anomaly, no pattern | One bad transcript |

Two rules. A SEV3 that has been true for a week is a SEV2 — duration is
severity. And anything involving another tenant's data starts at SEV1 until
proven otherwise.

---

## First moves by failure archetype (Chapter 1)

| Archetype | First signal you will actually see | First three moves |
|---|---|---|
| Eager interpreter | A complaint that the agent did something nobody asked for | Pull the session by correlation id; read the Policy decision log for that principal; tighten with a `forbid` |
| Lossy session | Incoherent output late in long sessions | Check session age and Session Storage size; check for a runtime version change mid-session; reproduce on a long golden case |
| Unverified hallucination | A customer quotes a fact or parameter that does not exist | Did retrieval return the source? Check grounding and schema validation; add the golden case |
| Runaway loop | Cost or latency anomaly, repeated tool calls in one trace | Kill switch for the session; check retry layers and loop limits; read hop count and path |
| Identity confusion | Wrong tenant's data, or an action under the wrong principal | SEV1: preserve evidence, Gateway `forbid`, find which layer the data came through |
| Silent regression | Quality down for days, or evaluations that stopped arriving | Check evaluation volume before scores; correlate with the change log; roll back first, diagnose second |

## Triage: the first ten minutes

Work these in order. The first three are the ones that answer most incidents.

**1. Did something change?**
```bash
# Runtime version and when it was updated
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id>
```
Check, in this order: runtime version deploys, model id changes, prompt
changes, tool definition changes, Guardrail version changes, Policy changes,
Knowledge Base syncs. Most agent incidents are a change with a delay.

**2. What do the quality metrics say?**
Open the quality dashboard. Look at the three starter evaluators (Helpfulness,
ToolSelectionAccuracy, GoalSuccessRate) over the last seven days, not the last hour. Look at evaluation *volume* as well as
scores — a flat line may be a stalled pipeline rather than stable quality.

**3. Can you reconstruct one bad session end to end?**
```bash
# With a correlation id from the complaint
python chapter-11/observability/cross_agent_trace.py --correlation-id <id>
```
If the customer gave you a timestamp instead of an id, filter traces by
tenant and time window. If the trace stops at an agent boundary, the
correlation id was not propagated — note it as a follow-up and reconstruct
by hand this once.

**4. Is the perimeter intact?**
Check Policy decision logs for unexpected permits and for a spike in denials.
Check Guardrail intervention counts. A quiet perimeter during an incident is
either good news or a perimeter that is not enforcing.

**5. Is the knowledge current?**
Check ingestion job status and per-document failure counts. An agent that
suddenly cannot answer a class of question often lost its source material.

---

## Playbooks by symptom

### Symptom: customer reports a wrong or unhelpful answer

1. Get a correlation id or the session timestamp and tenant.
2. Reconstruct the session. Read the whole transcript, not the summary.
3. Classify: retrieval miss, wrong tool, correct tool with wrong arguments,
   model reasoning error, or correct behavior that the customer disliked.
4. Run on-demand evaluation on that trace to see how the judges scored it.
5. If it represents a class, add it to the golden set before fixing it. The
   test comes first or it never comes.

### Symptom: quality metric dropped

1. Confirm it is real: check evaluation volume, and check whether the drop
   coincides with a traffic-mix change rather than a quality change.
2. Correlate with the change log. Model, prompt, tools, KB, Guardrails.
3. Read twenty low-scoring traces from after the drop and twenty from before.
4. If a deploy caused it, roll back first and diagnose second.

### Symptom: cross-tenant data appeared in a response

**SEV1. Page. Do not investigate alone.**

1. Preserve evidence: capture the trace, the session, and the tenant ids.
2. Contain: disable the agent endpoint if exposure is ongoing.
3. Determine the path: was it a tool result (Policy failure), Memory
   (namespace failure), Knowledge Base (metadata filter failure), or the
   model surfacing context it should not have had (Guardrails failure)?
4. Each path has a different fix and a different blast radius. Establish the
   path before estimating scope.
5. Involve security and legal per your incident process. Notification
   obligations are not an engineering decision.

### Symptom: the agent stopped escalating / started escalating everything

1. This is almost always tool selection. Check ToolSelectionAccuracy.
2. Compare tool descriptions against the last known-good version. A one-word
   edit to a description changes selection behavior.
3. Check whether a new tool was added whose description overlaps an existing
   one.

### Symptom: unexpected spend

1. Pull the payment receipts for the period, grouped by payee and purpose.
2. Check whether session limits were respected — they usually were. Then
   check the aggregate, which is where the problem usually is.
3. Look specifically for repeated purchases of the same durable resource
   across sessions.
4. Contain by revoking wallet authorization or tightening the daily cap;
   both are faster than a code change.
5. Tell finance the same day, with a number and a cause. They will find out
   regardless; the difference is whether they hear it from you.

### Symptom: no evaluation results for hours

1. Check the online evaluation configuration status.
2. Check that traces are still arriving — instrumentation may have broken
   in a deploy.
3. Check agent traffic volume. Zero sessions produces zero evaluations, and
   that is a different incident.

---

## Containment options, fastest first

| Action | Effect | Reversible |
|---|---|---|
| Revoke wallet authorization | Stops all spend immediately | Yes |
| Stop the whole agent (`kill-switch.md`) | Entry flag, Gateway `forbid`, `StopRuntimeSession`, payment sessions | Yes |
| Tighten a Cedar policy to `forbid` a tool | Stops one capability once the policy is live (measure the delay in your account; see `kill-switch.md`) | Yes |
| Point to previous Guardrail version | Restores previous filtering | Yes |
| Point the endpoint at the previous runtime version (`update-agent-runtime-endpoint --agent-runtime-version N`) | Restores previous behavior; **refreshes Session Storage** for affected sessions | Yes |
| Delete Memory records | Removes learned state | **No** |

Prefer the reversible, targeted options. Deleting memory during an incident
is a decision that cannot be undone and is almost never the fastest fix.

---

## After: the postmortem

Blameless, and with two agent-specific requirements beyond your normal
template:

- [ ] **Every incident produces at least one golden-set case.** This is the
      mechanism that prevents recurrence. Without it, the postmortem produces
      a document and a hope.
- [ ] **Name which layer should have caught it**: Policy, Guardrails,
      Automated Reasoning, Structured Outputs, Evaluations, or Registry. If
      the answer is "none of them", that gap is the most valuable finding in
      the postmortem.

Also record: time to detection, and how it was detected. Agent incidents
detected by customers rather than by monitoring are telling you where the
next investment goes.
