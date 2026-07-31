"""
quality_gate.py — Block a deploy when agent quality regresses.

Accompanies Chapter 10 of "AgentCore in Production: The Operator's Playbook".

This is the mechanism that would have stopped the Friday-night model swap in
Section 10.1. It runs the golden set against a candidate build, scores each
case individually with the synchronous `Evaluate` operation, compares the
scores to the recorded baseline, and exits non-zero when any metric
regresses beyond its tolerance.

WHY PER-CASE `Evaluate` AND NOT `StartBatchEvaluation`:
The batch path is cheaper per case, but `GetBatchEvaluation` returns only
per-evaluator aggregate statistics — total/completed/failed session counts
and one average score per evaluator. There is no per-session or per-category
field anywhere in that response (verified against the real service model).
A gate built on it therefore cannot do the one thing this gate exists to do:
notice that outage-escalation collapsed to zero while the average barely
moved, because most golden-set traffic is documentation lookups.

`Evaluate` is synchronous, takes one evaluator and one session's spans per
call, and accepts the ground truth inline via `evaluationReferenceInputs`.
That gives a score per (case, evaluator) pair, which is what a real
category breakdown requires. The cost is len(cases) * len(evaluators) calls
instead of one job — for a 12-case golden set and 3 evaluators that is 36
synchronous calls, which is an acceptable price for a gate that actually
gates. Scale the golden set with that multiplication in mind.

The design decisions worth copying:

    1. Absolute floors AND relative regressions. A metric can be above its
       floor and still have fallen off a cliff relative to last week. Both
       conditions fail the gate.

    2. Per-metric tolerances. Helpfulness moves around by a few points
       between runs; ToolSelectionAccuracy does not. One global threshold
       either lets real regressions through or blocks every build.

    3. Category-level gating. An aggregate that holds steady while one
       category collapses is the most common way a gate misses a
       regression — Acme's outage-escalation cases could go to zero while
       the average barely moves, because most traffic is documentation
       lookups. Scoring per case makes the category breakdown real, so a
       collapse in any CRITICAL_CATEGORIES entry fails the build on its own
       merits rather than being averaged away.

    4. The gate writes a new baseline only on an explicit flag. A gate that
       silently rebaselines on every run measures nothing over time.

Usage:
    python quality_gate.py \
        --runtime-arn arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme-support-candidate-f7g8h9j0k1 \
        --golden-set ../golden_set/acme_support_cases.jsonl \
        --baseline baseline.json

    # After a deliberate, reviewed quality change:
    python quality_gate.py ... --update-baseline

Exit codes:
    0 — all metrics within tolerance
    1 — at least one regression or floor violation
    2 — evaluation could not complete

Requires:
    pip install boto3
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import boto3

logger = logging.getLogger("quality_gate")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


# =========================================================================
# Thresholds
# =========================================================================

@dataclass(frozen=True)
class MetricPolicy:
    """Floor and regression tolerance for one metric."""
    floor: float                 # absolute minimum acceptable score
    max_regression: float        # largest tolerable drop vs baseline

    def evaluate(self, current: float, baseline: Optional[float]) -> list[str]:
        problems: list[str] = []
        if current < self.floor:
            problems.append(f"below floor: {current:.3f} < {self.floor:.3f}")
        if baseline is not None:
            drop = baseline - current
            if drop > self.max_regression:
                problems.append(
                    f"regression: {current:.3f} vs baseline {baseline:.3f} "
                    f"(drop {drop:.3f} > tolerance {self.max_regression:.3f})"
                )
        return problems


# Tolerances reflect how noisy each metric is between runs on an unchanged
# agent. Measure your own before trusting these numbers.
METRIC_POLICIES: dict[str, MetricPolicy] = {
    "Builtin.Helpfulness":           MetricPolicy(floor=0.70, max_regression=0.05),
    "Builtin.ToolSelectionAccuracy": MetricPolicy(floor=0.90, max_regression=0.03),
    "Builtin.GoalSuccessRate":       MetricPolicy(floor=0.70, max_regression=0.05),
    "AcmeEscalationAppropriateness": MetricPolicy(floor=0.85, max_regression=0.03),
    "AcmeDeterministicChecks":       MetricPolicy(floor=1.00, max_regression=0.00),
}

# Categories whose failure must never be averaged away. A regression in any
# of these fails the gate even if the overall numbers look fine. Because
# scoring is per case (see the module docstring), these are gated for real:
# a missing category is a hard failure, not a warning, since "the
# outage-escalation cases did not run" and "the outage-escalation cases
# passed" must never look the same to a release pipeline.
CRITICAL_CATEGORIES = {"outage-escalation", "tenant-isolation", "refund-boundary"}


# =========================================================================
# CloudWatch log group derivation
# =========================================================================
# This gate reads the spans it scores out of the runtime's own log group.
# Unlike evaluations/online_config.py — which also needs a CloudWatch
# *service name*, because CreateOnlineEvaluationConfig requires one — the
# synchronous Evaluate path takes the spans directly, so the log group is
# the only thing that has to be derived here.


def log_group_for_runtime(
    agent_runtime_arn: str, endpoint_name: str = "DEFAULT"
) -> str:
    """Derive the CloudWatch log group name for an AgentCore Runtime ARN.

    Convention: `/aws/bedrock-agentcore/runtimes/{agentRuntimeId}-{endpoint}`,
    where agentRuntimeId is the ARN segment after "runtime/". Pass
    --log-group-name explicitly if your runtime logs somewhere else.
    """
    resource_id = agent_runtime_arn.rsplit("/", 1)[-1]
    return f"/aws/bedrock-agentcore/runtimes/{resource_id}-{endpoint_name}"


# =========================================================================
# Running the golden set
# =========================================================================

def load_golden_set(path: str) -> list[dict[str, Any]]:
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def exercise_agent(runtime_arn: str, cases: list[dict], region: str) -> dict[str, str]:
    """Run every golden-set case against the candidate agent.

    Returns a mapping of case_id -> runtime session id. The session id is
    what the rest of this gate keys off: it is how the emitted spans are
    located in CloudWatch Logs, and it is what
    `evaluationReferenceInputs[].context.spanContext.sessionId` requires.
    Each case runs in its own session so session-level evaluators see clean
    boundaries and so one case's spans never bleed into another's score.
    """
    client = boto3.client("bedrock-agentcore", region_name=region)
    session_ids: dict[str, str] = {}

    for case in cases:
        case_id = case["case_id"]
        session_id = f"qualitygate-{case_id}-{int(time.time())}"
        try:
            response = client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                payload=json.dumps({"prompt": case["user_input"]}).encode(),
            )
            # A returned traceId confirms observability instrumentation is
            # active for this invocation. The session id is what batch
            # evaluation's CloudWatch discovery actually keys off — see
            # Chapter 10 Section 10.5.
            if response.get("traceId"):
                session_ids[case_id] = session_id
            else:
                logger.warning("%s: no trace id returned; is observability enabled?", case_id)
        except Exception as exc:
            logger.error("%s: invocation failed: %s", case_id, exc)

    return session_ids


# The attribute AgentCore's OTEL instrumentation stamps on every span, and
# the field the AWS reference implementation filters on. Note the shape:
# `session.id` nested under `attributes`, NOT a bare `session.id` and not
# `attributes.session_id`. Backticks are required in Logs Insights because
# the field name contains dots.
SESSION_ID_LOG_FIELD = "attributes.session.id"

# Spans land in one of two places depending on the runtime's span destination
# (see "Span destination for agents hosted in Amazon Bedrock AgentCore
# runtime"). Newly created agents in supported Regions write to their own log
# group; older agents write to the shared `aws/spans` group. Which one applies
# is not discoverable from the ARN, so both are queried and the results are
# concatenated — exactly what the AWS on-demand evaluation walkthrough does.
SHARED_SPANS_LOG_GROUP = "aws/spans"


def _query_session_logs(
    logs_client: Any,
    log_group_name: str,
    session_id: str,
    lookback_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Run the documented Logs Insights query for one session, one log group.

    Returns the parsed `@message` documents. A missing log group is not an
    error: an agent using the shared span destination has no per-agent group
    and vice versa, so one of the two queries is expected to come back empty.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=lookback_minutes)

    # `ispresent(scope.name)` is what distinguishes a span record from the
    # application's own stdout, which lands in the same log group. Without it,
    # ordinary structured log lines get passed to Evaluate as if they were
    # spans.
    query = f"""fields @timestamp, @message
        | filter ispresent(scope.name) and ispresent(`{SESSION_ID_LOG_FIELD}`)
        | filter `{SESSION_ID_LOG_FIELD}` = "{session_id}"
        | sort @timestamp asc"""

    try:
        query_id = logs_client.start_query(
            logGroupName=log_group_name,
            startTime=int(start.timestamp()),
            endTime=int(now.timestamp()),
            queryString=query,
        )["queryId"]
    except logs_client.exceptions.ResourceNotFoundException:
        logger.debug("log group %s does not exist; skipping", log_group_name)
        return []

    while True:
        result = logs_client.get_query_results(queryId=query_id)
        # Every terminal state, not just the happy path: a query left on
        # "Timeout" or "Unknown" would otherwise poll forever.
        if result["status"] in (
            "Complete", "Failed", "Cancelled", "Timeout", "Unknown"
        ):
            break
        time.sleep(1)

    if result["status"] != "Complete":
        raise RuntimeError(
            f"Logs Insights query on {log_group_name} ended as {result['status']}"
        )

    docs: list[dict[str, Any]] = []
    for row in result.get("results", []):
        for field in row:
            if field.get("field") != "@message":
                continue
            raw = (field.get("value") or "").strip()
            if not raw.startswith("{"):
                continue
            try:
                docs.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return docs


def fetch_session_spans(
    session_id: str,
    log_group_name: str,
    region: str,
    max_spans: int = 1000,
) -> list[dict[str, Any]]:
    """Read the OTEL spans a session emitted, for handing to `Evaluate`.

    `Evaluate` does not discover traffic on its own — unlike
    `StartBatchEvaluation`, which points at a CloudWatch Logs source and finds
    sessions itself, `Evaluate` requires the caller to hand it the spans in
    `evaluationInput.sessionSpans` (min 1, max 1000 documents per call, per
    the service model). Downloading them first is the documented workflow;
    see "Getting started with on-demand evaluation" > "Download span-logs
    from CloudWatch" in the AgentCore developer guide.

    Both candidate span destinations are queried and the results combined,
    because which one a given runtime uses depends on when and where it was
    created.
    """
    logs_client = boto3.client("logs", region_name=region)

    spans = _query_session_logs(logs_client, log_group_name, session_id)
    spans += _query_session_logs(logs_client, SHARED_SPANS_LOG_GROUP, session_id)

    if len(spans) > max_spans:
        logger.warning(
            "session %s produced %d span documents; truncating to the %d the "
            "Evaluate API accepts", session_id, len(spans), max_spans,
        )
        spans = spans[:max_spans]
    return spans


def _reference_inputs(session_id: str, ground_truth: dict[str, Any]) -> list[dict]:
    """Map a golden-set case's ground truth onto evaluationReferenceInputs.

    The golden set carries exactly the three forms `Evaluate` accepts, so
    the mapping is one-to-one:

        reference_answer        -> expectedResponse.text
        behavioral_assertions   -> assertions[].text
        expected_tool_trajectory-> expectedTrajectory.toolNames

    `context.spanContext.sessionId` is required on every reference input; it
    is what ties the reference back to the spans submitted alongside it.
    Fields the chosen evaluator does not consume come back in the response's
    `ignoredReferenceInputFields`, which is informational, not an error.
    """
    reference: dict[str, Any] = {
        "context": {"spanContext": {"sessionId": session_id}},
    }
    if ground_truth.get("reference_answer"):
        reference["expectedResponse"] = {"text": ground_truth["reference_answer"]}
    if ground_truth.get("behavioral_assertions"):
        reference["assertions"] = [
            {"text": a} for a in ground_truth["behavioral_assertions"]
        ]
    if ground_truth.get("expected_tool_trajectory"):
        reference["expectedTrajectory"] = {
            "toolNames": list(ground_truth["expected_tool_trajectory"])
        }
    return [reference]


@dataclass
class CaseScore:
    case_id: str
    category: str
    evaluator_id: str
    value: Optional[float]
    label: Optional[str] = None
    error: Optional[str] = None


def run_synchronous_evaluations(
    session_map: dict[str, str],
    case_index: dict[str, dict],
    evaluators: list[str],
    log_group_name: str,
    region: str,
    settle_seconds: int = 180,
) -> list[CaseScore]:
    """Score every (case, evaluator) pair with a synchronous Evaluate call.

    This is the Decision-1 shape: one `Evaluate` per case per evaluator,
    which is what makes a real per-category breakdown possible. See the
    module docstring for why the cheaper batch path cannot do this.

    A case whose spans cannot be found, or whose evaluation returns an
    error, is recorded as a CaseScore with `value=None` and an `error`
    string rather than being silently dropped — a case that did not score
    must never be indistinguishable from a case that scored well.
    """
    client = boto3.client("bedrock-agentcore", region_name=region)

    # Spans take "a couple of minutes" to populate in CloudWatch per the AWS
    # on-demand evaluation walkthrough, so the default here is three minutes
    # rather than a token pause. Scoring too early yields empty span sets,
    # which this gate reports as unscored cases — visible, but a wasted run.
    if settle_seconds:
        logger.info("waiting %ds for spans to reach CloudWatch...", settle_seconds)
        time.sleep(settle_seconds)

    scores: list[CaseScore] = []
    for case_id, session_id in sorted(session_map.items()):
        case = case_index[case_id]
        category = case.get("category", "uncategorized")
        spans = fetch_session_spans(session_id, log_group_name, region)
        if not spans:
            logger.error("%s: no spans found for session %s", case_id, session_id)
            for evaluator_id in evaluators:
                scores.append(CaseScore(case_id, category, evaluator_id, None,
                                        error="no spans found"))
            continue

        references = _reference_inputs(session_id, case.get("ground_truth", {}))
        for evaluator_id in evaluators:
            try:
                response = client.evaluate(
                    evaluatorId=evaluator_id,
                    evaluationInput={"sessionSpans": spans},
                    evaluationReferenceInputs=references,
                )
            except Exception as exc:
                logger.error("%s / %s: Evaluate failed: %s", case_id, evaluator_id, exc)
                scores.append(CaseScore(case_id, category, evaluator_id, None,
                                        error=str(exc)))
                continue

            results = response.get("evaluationResults") or []
            if not results:
                scores.append(CaseScore(case_id, category, evaluator_id, None,
                                        error="empty evaluationResults"))
                continue
            result = results[0]
            if result.get("errorCode") or result.get("errorMessage"):
                scores.append(CaseScore(
                    case_id, category, evaluator_id, None,
                    error=f"{result.get('errorCode')}: {result.get('errorMessage')}"))
                continue
            scores.append(CaseScore(
                case_id, category, evaluator_id,
                value=None if result.get("value") is None else float(result["value"]),
                label=result.get("label"),
            ))
            logger.info("  %s / %s -> %s", case_id, evaluator_id, result.get("value"))

    return scores


# =========================================================================
# Aggregation
# =========================================================================

def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4)


def aggregate(scores: list[CaseScore]) -> dict[str, Any]:
    """Fold per-case scores into overall and per-category averages.

    Cases that failed to score contribute to `errors` and to the
    `not_scored` counts, and are excluded from the averages — averaging a
    missing score as zero would turn an infrastructure problem into a
    quality regression, and averaging it as one would hide a real one.
    """
    overall_values: dict[str, list[float]] = {}
    category_values: dict[str, dict[str, list[float]]] = {}
    not_scored: dict[str, int] = {}
    errors: list[str] = []

    for s in scores:
        if s.value is None:
            not_scored[s.evaluator_id] = not_scored.get(s.evaluator_id, 0) + 1
            errors.append(f"{s.case_id} / {s.evaluator_id}: {s.error}")
            continue
        overall_values.setdefault(s.evaluator_id, []).append(s.value)
        (category_values.setdefault(s.category, {})
                        .setdefault(s.evaluator_id, []).append(s.value))

    return {
        "overall": {k: _mean(v) for k, v in sorted(overall_values.items())},
        "by_category": {
            cat: {ev: _mean(vals) for ev, vals in sorted(evs.items())}
            for cat, evs in sorted(category_values.items())
        },
        "sample_counts": {k: len(v) for k, v in sorted(overall_values.items())},
        "category_sample_counts": {
            cat: {ev: len(vals) for ev, vals in sorted(evs.items())}
            for cat, evs in sorted(category_values.items())
        },
        "not_scored": not_scored,
        "errors": errors,
    }


# =========================================================================
# The gate
# =========================================================================

def check_gate(current: dict[str, Any], baseline: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compare current scores against baseline and policy. Returns (passed, findings)."""
    findings: list[str] = []
    base_overall = baseline.get("overall", {})

    for metric, score in sorted(current.get("overall", {}).items()):
        policy = METRIC_POLICIES.get(metric)
        if policy is None:
            findings.append(f"NOTE  {metric}: no policy defined; not gated")
            continue
        problems = policy.evaluate(score, base_overall.get(metric))
        for p in problems:
            findings.append(f"FAIL  {metric}: {p}")
        if not problems:
            findings.append(f"ok    {metric}: {score:.3f}")

    # Critical categories are gated separately so a healthy average cannot
    # hide a collapsed category. A critical category with no scored cases is
    # a FAIL, not a warning: "we did not check" must not read the same as
    # "we checked and it was fine".
    base_cat = baseline.get("by_category", {})
    for category in sorted(CRITICAL_CATEGORIES):
        current_metrics = current.get("by_category", {}).get(category, {})
        if not current_metrics:
            findings.append(
                f"FAIL  category '{category}': no cases scored — a critical "
                "category that did not run cannot be treated as passing"
            )
            continue
        for metric, score in sorted(current_metrics.items()):
            policy = METRIC_POLICIES.get(metric)
            if policy is None:
                continue
            prior = base_cat.get(category, {}).get(metric)
            problems = policy.evaluate(score, prior)
            for p in problems:
                findings.append(f"FAIL  [{category}] {metric}: {p}")
            if not problems:
                findings.append(f"ok    [{category}] {metric}: {score:.3f}")

    # Cases that could not be scored at all are surfaced, never swallowed.
    for line in current.get("errors", []):
        findings.append(f"WARN  unscored: {line}")

    passed = not any(f.startswith("FAIL") for f in findings)
    return passed, findings


# =========================================================================
# CLI
# =========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Agent quality gate for CI/CD.")
    parser.add_argument("--runtime-arn", required=True, help="Candidate agent runtime ARN.")
    parser.add_argument("--golden-set", required=True, help="Path to the golden set JSONL.")
    parser.add_argument("--baseline", default="baseline.json", help="Baseline scores file.")
    parser.add_argument("--evaluators", nargs="*", default=[
        "Builtin.Helpfulness",
        "Builtin.ToolSelectionAccuracy",
        "Builtin.GoalSuccessRate",
    ])
    parser.add_argument("--endpoint-name", default="DEFAULT",
                        help="Runtime endpoint name, used to derive the CloudWatch log group.")
    parser.add_argument("--log-group-name", default=None,
                        help="Override the derived CloudWatch log group the spans are read from.")
    parser.add_argument("--settle-seconds", type=int, default=180,
                        help="How long to wait for spans to reach CloudWatch before "
                             "scoring. AWS documents a couple of minutes of lag; "
                             "scoring earlier just produces unscored cases.")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Write current scores as the new baseline. Use only after review.")
    parser.add_argument("--region", default=AWS_REGION)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cases = load_golden_set(args.golden_set)
    logger.info("golden set: %d cases", len(cases))

    try:
        log_group_name = args.log_group_name or log_group_for_runtime(
            args.runtime_arn, args.endpoint_name
        )

        session_map = exercise_agent(args.runtime_arn, cases, args.region)
        if not session_map:
            logger.error("no sessions produced; cannot evaluate")
            return 2

        # case_id -> case, for the cases that actually produced a session.
        case_index = {c["case_id"]: c for c in cases if c["case_id"] in session_map}
        scores = run_synchronous_evaluations(
            session_map, case_index, args.evaluators, log_group_name,
            args.region, settle_seconds=args.settle_seconds,
        )
    except Exception as exc:
        logger.error("evaluation failed: %s", exc)
        return 2

    if not scores:
        logger.error("no evaluation results produced")
        return 2

    current = aggregate(scores)

    baseline: dict[str, Any] = {}
    if os.path.exists(args.baseline):
        with open(args.baseline) as f:
            baseline = json.load(f)
    else:
        logger.warning("no baseline at %s; floors will be checked, regressions cannot be",
                       args.baseline)

    passed, findings = check_gate(current, baseline)

    print()
    print("=" * 64)
    for line in findings:
        print(line)
    print("=" * 64)
    print(json.dumps({"overall": current["overall"],
                      "by_category": current["by_category"]}, indent=2))

    if args.update_baseline:
        with open(args.baseline, "w") as f:
            json.dump(current, f, indent=2)
        logger.info("baseline updated at %s", args.baseline)

    if not passed:
        print("\nQUALITY GATE FAILED — deploy blocked")
        return 1

    print("\nquality gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
