"""
ab_test.py — Configuration-bundle A/B tests, run as experiments.

Accompanies Chapter 10 (Section 10.9 and 10.10) of "AgentCore in Production:
The Operator's Playbook", 2nd edition.

Three things this module enforces that the console does not:

1. The experiment is written down before it runs. `ExperimentPlan` requires a
   hypothesis, a refutation criterion, the evaluator that decides, and a
   minimum sample per variant. `start_ab_test` refuses to create a test
   without one.

2. Control and treatment are two VERSIONS OF THE SAME configuration bundle.
   `agentcore promote` only works between versions of the same bundle; the
   A/B prerequisites page also allows two separate bundles, and a test built
   that way can run but cannot be promoted (author's lab, 2026-08-05 post).

3. Promotion is a recorded decision. `decide` returns "promote" only on a
   significant result with enough sessions. Anything else is "no evidence",
   and promoting anyway requires a reason that ends up in the record.

API NOTE: shapes verified offline against botocore 1.43.10 service models
(`bedrock-agentcore-control` CreateConfigurationBundle /
UpdateConfigurationBundle; `bedrock-agentcore` CreateABTest / GetABTest).
Not run against a live account for the 2nd edition.

Quotas (Oct 2026): one A/B test per gateway, two variants (C and T1), 20
active tests per account. Bundle and test names match [a-zA-Z][a-zA-Z0-9_]*
(no hyphens).

Requires:
    pip install boto3
"""

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


# =========================================================================
# The experiment, written before it runs
# =========================================================================

@dataclass(frozen=True)
class ExperimentPlan:
    """What you believe, what would prove you wrong, and how much data it takes.

    Fill this in and commit it before the test starts. Section 10.10 explains
    why: an optimizer, or an A/B console, will always give you a number. The
    plan is what tells you whether the number answers your question.
    """
    name: str
    hypothesis: str
    refutation_criterion: str
    deciding_evaluator: str
    min_sessions_per_variant: int
    noise_floor: Optional[float] = None   # measured: unchanged config run twice
    variance_controls: tuple[str, ...] = ()  # e.g. ("memory disabled",)

    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.hypothesis.strip():
            found.append("hypothesis is empty")
        if not self.refutation_criterion.strip():
            found.append("refutation_criterion is empty: what result would prove you wrong?")
        if self.min_sessions_per_variant < 30:
            found.append(
                "min_sessions_per_variant < 30; with a baseline above 0.9, plan "
                "for hundreds per variant (the 2026-08-05 lab reached p=0.38 at 103/39)"
            )
        if self.noise_floor is None:
            found.append("noise_floor not measured: run the unchanged config twice first")
        return found


# =========================================================================
# Bundle versions: control and treatment in ONE bundle
# =========================================================================

def _components(runtime_arn: str, configuration: dict[str, Any]) -> dict:
    return {runtime_arn: {"configuration": configuration}}


def create_variant_versions(
    control_client,
    bundle_name: str,
    runtime_arn: str,
    control_configuration: dict[str, Any],
    treatment_configuration: dict[str, Any],
) -> dict[str, str]:
    """Create one bundle with two versions: control, then treatment.

    The treatment version's parent is the control version, so the lineage
    reads like a git commit on `mainline`, and `promote` can later move
    between them.
    """
    created = control_client.create_configuration_bundle(
        bundleName=bundle_name,
        components=_components(runtime_arn, control_configuration),
        commitMessage="Control: current production configuration",
        clientToken=str(uuid.uuid4()),
    )
    updated = control_client.update_configuration_bundle(
        bundleId=created["bundleId"],
        components=_components(runtime_arn, treatment_configuration),
        parentVersionIds=[created["versionId"]],
        commitMessage="Treatment: candidate configuration",
        clientToken=str(uuid.uuid4()),
    )
    return {
        "bundle_arn": created["bundleArn"],
        "bundle_id": created["bundleId"],
        "control_version": created["versionId"],
        "treatment_version": updated["versionId"],
    }


def ab_test_request(
    plan: ExperimentPlan,
    gateway_arn: str,
    role_arn: str,
    online_evaluation_config_arn: str,
    versions: dict[str, str],
    treatment_weight: int = 10,
) -> dict[str, Any]:
    """Build the CreateABTest request. Both variants reference one bundle."""
    if not 1 <= treatment_weight <= 99:
        raise ValueError("treatment_weight must be between 1 and 99")

    def variant(name: str, weight: int, version: str) -> dict[str, Any]:
        return {
            "name": name,
            "weight": weight,
            "variantConfiguration": {
                "configurationBundle": {
                    "bundleArn": versions["bundle_arn"],
                    "bundleVersion": version,
                }
            },
        }

    return {
        "name": plan.name,
        "description": plan.hypothesis[:200],
        "gatewayArn": gateway_arn,
        "roleArn": role_arn,
        "variants": [
            variant("C", 100 - treatment_weight, versions["control_version"]),
            variant("T1", treatment_weight, versions["treatment_version"]),
        ],
        "evaluationConfig": {"onlineEvaluationConfigArn": online_evaluation_config_arn},
        "enableOnCreate": True,
        "clientToken": str(uuid.uuid4()),
    }


def start_ab_test(data_client, plan: ExperimentPlan, request: dict[str, Any]) -> dict:
    """Create the A/B test, but only for a plan that was written down."""
    problems = plan.problems()
    if problems:
        raise ValueError("Experiment plan is incomplete:\n  " + "\n  ".join(problems))
    if len({v["variantConfiguration"]["configurationBundle"]["bundleArn"]
            for v in request["variants"]}) != 1:
        raise ValueError("control and treatment must be versions of the same bundle")
    logger.info("Starting A/B test %s: %s", plan.name, plan.hypothesis)
    return data_client.create_ab_test(**request)


# =========================================================================
# Reading the result, and deciding
# =========================================================================

@dataclass
class Decision:
    experiment: str
    verdict: str            # "promote", "no evidence", "promote by judgment", "reject"
    evaluator: str
    control_n: int
    treatment_n: int
    control_mean: Optional[float]
    treatment_mean: Optional[float]
    p_value: Optional[float]
    observed_treatment_share: Optional[float]
    reason: str
    decided_at: str


def decide(
    plan: ExperimentPlan,
    ab_test: dict[str, Any],
    judgment_reason: Optional[str] = None,
) -> Decision:
    """Turn a GetABTest response into a recorded decision.

    Uses the service's p-value as reported. Do not recompute it: in the
    author's lab, Fisher, Yates, and Welch gave 0.19, 0.28, and 0.014 on the
    same counts where the service reported 0.38.
    """
    metrics = (ab_test.get("results") or {}).get("evaluatorMetrics", [])
    metric = next((m for m in metrics
                   if m["evaluatorArn"].endswith(plan.deciding_evaluator)), None)
    now = datetime.now(timezone.utc).isoformat()
    if metric is None or not metric.get("variantResults"):
        return Decision(plan.name, "no evidence", plan.deciding_evaluator, 0, 0,
                        None, None, None, None, "no results for the deciding evaluator", now)

    control = metric["controlStats"]
    treatment = metric["variantResults"][0]
    total = control["sampleSize"] + treatment["sampleSize"]
    share = treatment["sampleSize"] / total if total else None
    enough = min(control["sampleSize"], treatment["sampleSize"]) >= plan.min_sessions_per_variant
    better = treatment["mean"] > control["mean"]

    if treatment.get("isSignificant") and enough and better:
        verdict, reason = "promote", "significant improvement at planned sample size"
    elif treatment.get("isSignificant") and enough and not better:
        verdict, reason = "reject", "significant regression"
    elif judgment_reason:
        verdict, reason = "promote by judgment", judgment_reason
    else:
        verdict, reason = "no evidence", (
            "not significant" if enough else "below planned sample size")

    return Decision(plan.name, verdict, plan.deciding_evaluator,
                    control["sampleSize"], treatment["sampleSize"],
                    control["mean"], treatment["mean"], treatment.get("pValue"),
                    share, reason, now)


# =========================================================================
# The Friday-night swap, run as an experiment instead of a deploy
# =========================================================================

FRIDAY_SWAP_PLAN = ExperimentPlan(
    name="acmeSupportModelSwap",
    hypothesis=(
        "The candidate model keeps ToolSelectionAccuracy within the noise floor "
        "of the current model on live traffic."
    ),
    refutation_criterion=(
        "Treatment ToolSelectionAccuracy below control by more than the noise "
        "floor, or any significant drop, refutes it; the swap does not ship."
    ),
    deciding_evaluator="Builtin.ToolSelectionAccuracy",
    min_sessions_per_variant=300,
    noise_floor=0.02,
    variance_controls=("long-term memory retrieval disabled for the test window",),
)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme_support-a1b2c3d4e5"
    versions = {
        "bundle_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:configuration-bundle/acmeSupportConfig-a1b2c3d4e5",
        "bundle_id": "acmeSupportConfig-a1b2c3d4e5",
        "control_version": "00000000-0000-0000-0000-000000000001",
        "treatment_version": "00000000-0000-0000-0000-000000000002",
    }
    request = ab_test_request(
        FRIDAY_SWAP_PLAN,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw-a1b2c3d4e5",
        role_arn="arn:aws:iam::111122223333:role/acme-support-abtest-role",
        online_evaluation_config_arn=(
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:"
            "online-evaluation-config/acme_support_online_eval-a1b2c3d4e5"
        ),
        versions=versions,
    )
    print(json.dumps(request, indent=2))

    # The author's August 2026 lab result, replayed through decide():
    lab = {"results": {"evaluatorMetrics": [{
        "evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate",
        "controlStats": {"variantName": "C", "sampleSize": 103, "mean": 0.9417},
        "variantResults": [{"variantName": "T1", "sampleSize": 39, "mean": 1.0,
                            "percentChange": 6.2, "pValue": 0.38, "isSignificant": False}],
    }]}}
    lab_plan = ExperimentPlan(
        name="devopsPromptTest", hypothesis="Recommended prompt raises goal success.",
        refutation_criterion="No significant improvement at 100+ sessions per variant.",
        deciding_evaluator="Builtin.GoalSuccessRate", min_sessions_per_variant=100,
        noise_floor=0.05,
    )
    print(json.dumps(asdict(decide(lab_plan, lab)), indent=2))
