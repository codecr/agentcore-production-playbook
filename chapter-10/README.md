# Chapter 10: AgentCore Evaluations

This folder accompanies Chapter 10 of *AgentCore in Production: The Operator's Playbook* (2nd edition, October 2026).

## What changed in the 2nd edition

- **New `optimization/` folder** for Sections 10.9 and 10.10: configuration
  bundle versions, A/B tests run from a written `ExperimentPlan`, a `decide()`
  function that separates statistical promotion from judgment calls, and
  `audit_optimizer.py`, three checks to run before trusting any optimizer's
  reported improvement.
- **Online evaluation config renamed** `acme_support_online_eval`. The API
  pattern for the name is `[a-zA-Z][a-zA-Z0-9_]{0,47}`; the 1st edition's
  hyphenated name would have been rejected.
- **Acme's default volume is ~330 sessions/day** (10,000 conversations a
  month, per the book's canon). The volume target alone would sample 90.91%;
  the starter config caps it at 20% for cost (Section 10.5, Appendix C).
- Batch evaluation is GA (June 17, 2026); Evaluations runs in all 22
  AgentCore Regions; framework support expanded (see Prerequisites).

## What's here

```
chapter-10/
├── evaluations/
│   ├── online_config.py          # Online evaluation config: sampling, filters, evaluators
│   └── custom_evaluators.py      # 2 LLM-judge evaluators + 1 code-based Lambda evaluator
├── golden_set/
│   └── acme_support_cases.jsonl  # 12 ground-truth cases with expected tool trajectories
├── ci/
│   └── quality_gate.py           # CI gate: run golden set, compare to baseline, block on regression
├── optimization/
│   ├── ab_test.py                # ExperimentPlan, one bundle / two versions, CreateABTest, decide()
│   └── audit_optimizer.py        # prompt changed? empty inferences? optimizer errors?
├── monitoring/
│   └── evaluation-alarms.yaml    # CloudWatch alarms on quality metrics
└── README.md
```

## What this delivers

**An online evaluation configuration as code.** `online_config.py` builds the
configuration the chapter argues for: the three starter evaluators
(Helpfulness, ToolSelectionAccuracy, GoalSuccessRate), a sampling rate
derived from traffic volume rather than guessed, and no filters until a
baseline exists. It validates the configuration before sending it, including
a check that fails when a team selects more than five evaluators on a new
config.

**Custom evaluators of both kinds.** Two LLM-as-a-judge evaluators for
questions that need reading and judgment — escalation appropriateness and
billing accuracy — each with a rating scale whose points have definitions.
Plus one code-based Lambda evaluator that runs three deterministic checks:
cross-tenant identifier leakage, monetary amounts not supported by any tool
result, and internal markers in customer-facing text. The code-based
evaluator runs locally with no AWS access:

```bash
python evaluations/custom_evaluators.py
# passing: score=1.0 — All deterministic checks passed.
# failing: score=0.0 — Failed checks: response mentions tenant identifier(s)
#          outside this session: ['ACM-9B22D1-M4']; response states monetary
#          amount(s) not present in any tool result: ['980.00']
# formatting_drift: score=1.0 — All deterministic checks passed.
# missing_tenant_id: score=0.0 — Failed checks: cross-tenant check skipped:
#          no sessionTenantId provided; failing closed because tenant
#          isolation could not be confirmed
```

The amount check compares monetary figures numerically (not as captured
strings), so `"$500"` vs `"$500.00"` is not a false-positive mismatch. The
cross-tenant check fails closed — a session with no `sessionTenantId` scores
0 with an explicit reason, rather than silently scoring 1.0 as if isolation
had been confirmed.

**A golden set with real ground truth.** Twelve cases covering outage
escalation, billing proration, tenant isolation, refund boundaries,
documentation lookups, denied topics, and multi-turn persistence. Each case
carries all three ground-truth forms that AgentCore Evaluations supports at
GA: a reference answer, behavioral assertions, and an expected tool
trajectory. Two cases are deliberate regression probes for failures
described earlier in the book.

**A quality gate for CI/CD.** `quality_gate.py` runs the golden set against a
candidate build, scores **each case individually** with the synchronous
`Evaluate` operation, and blocks the deploy when any metric falls below its
floor or regresses beyond its tolerance. It refuses to rebaseline unless you
pass `--update-baseline` explicitly.

Critical categories are gated separately and for real, so a healthy average
cannot hide a collapsed category. That is the reason the gate scores per case
instead of submitting one batch job: `GetBatchEvaluation` returns only
aggregate per-evaluator statistics, with no per-session or per-category field
anywhere in the response, so a batch-based gate structurally cannot notice
that outage-escalation went to zero while the average held steady. The trade
is cost — `len(cases) × len(evaluators)` synchronous calls instead of one job
(36 calls for this 12-case golden set with three evaluators). Scale the golden
set with that multiplication in mind. A critical category that produced no
scored cases is treated as a **failure**, not a warning: "we did not check"
must never read the same as "we checked and it was fine".

**An optimization loop you can audit.** `optimization/ab_test.py` creates
control and treatment as two versions of the same configuration bundle
(`agentcore promote` only works between versions of one bundle), builds the
`CreateABTest` request, and refuses to start without an `ExperimentPlan`
that names a hypothesis, a refutation criterion, the deciding evaluator, a
minimum sample per variant, and a measured noise floor. `decide()` reads a
`GetABTest` response and returns `promote` only for a significant improvement
at the planned sample size; anything else is `no evidence`, or `promote by
judgment` with a recorded reason. Running the module replays the author's
August 2026 lab result (103 vs 39 sessions, p = 0.38) and returns
`no evidence`. `optimization/audit_optimizer.py` implements the three checks
from Section 10.10. Both run offline:

```bash
python optimization/ab_test.py
python optimization/audit_optimizer.py
```

Quotas worth knowing (October 2026): one A/B test per gateway, two variants
(C and T1), 20 sessions sampled per recommendation (not adjustable), 25
evaluators per online evaluation configuration.

**Alarms for what the gate cannot catch.** Three alarm shapes: sustained low
scores, sudden drops via anomaly detection bands, and — the important one —
an alarm that fires when evaluation results stop arriving at all. A quality
dashboard that stopped updating looks identical to a quality dashboard with
no problems.

## Prerequisites

AgentCore Evaluations reads traces, which means the agent has to be emitting
them:

1. **Instrumentation.** OpenTelemetry plus OpenInference, so spans, tool
   calls, and session boundaries are captured in a form AgentCore can read.
2. **A supported framework.** Python: Strands, LangGraph, OpenAI Agents,
   LlamaIndex, Google ADK, Claude Agent SDK. TypeScript (since September
   2026): Strands, LangGraph, OpenAI Agents, Vercel AI SDK. Anything else
   works if it emits the OpenTelemetry GenAI or OpenInference semantic
   conventions. Agents created after July 20, 2026 write spans to their own
   log group (`/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint>`), not
   `aws/spans`.
3. **IAM.** Two access models are involved — the service reading your traces,
   and your principals invoking evaluation APIs. Configure both.
4. **A supported Region.** Nine at GA (March 2026); all 22 AgentCore
   Regions as of October 2026. Optimization (Recommendations, A/B tests,
   configuration bundles) is available everywhere except GovCloud. Check the
   current regions table.

Without instrumentation there is nothing to evaluate, and the failure is
quiet: configurations succeed, jobs run, and no results appear.

## Quickstart

```bash
pip install boto3
export AWS_REGION=us-east-1

# 1. Create the online evaluation configuration
python evaluations/online_config.py     # prints the request for review
# then, in your deploy pipeline:
#   EvaluationConfigClient().create(acme_support_config(
#       agent_runtime_arn=RUNTIME_ARN,
#       evaluation_execution_role_arn=EVAL_ROLE_ARN,
#       sessions_per_day=330,
#   ))
#
# The CloudWatch log group and service name AgentCore Evaluations reads from
# are derived from RUNTIME_ARN by convention (AgentCore Runtime's
# "{name}-{10 alphanumeric chars}" id shape); pass log_group_names /
# service_names explicitly to OnlineEvaluationConfig if your runtime id
# doesn't match that shape.

# 2. Deploy the alarms (one stack per agent)
aws cloudformation deploy \
  --template-file monitoring/evaluation-alarms.yaml \
  --stack-name agent-quality-acme-support \
  --parameter-overrides \
    AgentName=acme-support \
    NotificationTopicArn=arn:aws:sns:us-east-1:111122223333:platform-alerts

# 3. Wire the gate into CI
python ci/quality_gate.py \
  --runtime-arn arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme-support-candidate-f7g8h9j0k1 \
  --golden-set golden_set/acme_support_cases.jsonl \
  --baseline ci/baseline.json
# --log-group-name / --endpoint-name override the log group the gate would
# otherwise derive from --runtime-arn. --settle-seconds controls how long it
# waits for spans to reach CloudWatch before scoring (default 30).
```

The gate exits 1 on regression, which is what makes it a gate rather than a
report.

## Establishing the first baseline

A gate with no baseline can check floors but not regressions, so the first
run is a measurement, not a verdict:

```bash
# On a build you already trust, in a quiet period
python ci/quality_gate.py ... --update-baseline
```

Re-baseline only after a deliberate, reviewed quality change — a new prompt,
a new model, a new tool. Re-baselining because the gate is failing converts
the gate into a rubber stamp, which is Chapter 10 anti-pattern number four.

## API note

AgentCore Evaluations reached GA on 2026-03-31; batch evaluation,
Recommendations, and A/B testing reached GA on 2026-06-17. Shapes in this
folder were re-checked offline against botocore 1.43.10 for the 2nd edition
(`repo/_verify/ch10_optimization.py`); `online_config.py` was run against a live runtime on 2026-11-11; the optimization and A/B code was not re-run against a live
account. The control-plane and data-plane
parameter names in this folder follow the current API shape (verified
against the `bedrock-agentcore` / `bedrock-agentcore-control` botocore
service models); re-verify against the current API reference before
deploying, and prefer the `agentcore-cli` or CloudFormation if your team
already standardizes on them.

Two things worth knowing before you build on this code directly:

- **Online evaluation reads from CloudWatch Logs, not from a runtime ARN.**
  `CreateOnlineEvaluationConfig` takes a `dataSourceConfig.cloudWatchLogs`
  source (log group names + a service name), which this folder derives from
  the agent runtime ARN by convention — see
  `evaluations/online_config.py::cloudwatch_source_for_runtime`. The same is
  true of `StartBatchEvaluation`, which is why it discovers sessions itself.
- **`GetBatchEvaluation` reports aggregate scores only, which is why the gate
  does not use it.** Its `evaluationResults` field is a per-evaluator summary
  (average score, session counts) — there is no per-session or per-category
  breakdown anywhere in that response. A gate built on it cannot detect the
  single most important regression shape: one critical category collapsing
  while the average holds steady. `ci/quality_gate.py` therefore scores each
  golden-set case with a synchronous `Evaluate` call instead, passing the
  session's spans in `evaluationInput.sessionSpans` and the case's ground
  truth in `evaluationReferenceInputs`. `Evaluate` does not discover traffic
  on its own, so the gate reads each session's spans out of the runtime's log
  group first (`fetch_session_spans`).

## License

Code and configurations in this folder are MIT licensed. The accompanying
book chapter is copyrighted; the code is explicitly intended for reuse.
