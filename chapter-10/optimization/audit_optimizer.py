"""
audit_optimizer.py — Check an optimizer's claimed improvement before believing it.

Accompanies Chapter 10 (Section 10.10) of "AgentCore in Production: The
Operator's Playbook", 2nd edition.

Source of the three checks: the author's Bedrock Advanced Prompt Optimization
lab (gerardo.dev, 2026-10-26). In one day:

  - a job reported +45% (0.575 -> 0.833) for a returned prompt identical to
    the original; 12 of 60 original-side inferences were empty and scored 0;
  - a job finished Completed / SUCCESS while its optimizer model failed 684 of
    689 calls, visible only in CloudWatch InvocationClientErrors.

The checks are deliberately boring: did the text change, how many inferences
came back empty, and did the optimizer itself log errors. The same three
questions apply to an AgentCore recommendation or a FailureAnalysis insight:
compare the proposed artifact to the current one, check the sample it was
built from, and check that the machinery actually ran.

Requires:
    pip install boto3
"""

from datetime import datetime
from typing import Any, Iterable


def prompt_changed(original: str, optimized: str) -> bool:
    """True only if the returned prompt differs after whitespace normalization."""
    return " ".join(original.split()) != " ".join(optimized.split())


def empty_inferences(samples: Iterable[dict[str, Any]]) -> int:
    """Count samples whose inference produced no output tokens.

    An empty inference enters an average as one more 0.0. Twelve of them in
    sixty were enough to manufacture a 45% "improvement".
    """
    return sum(1 for s in samples if not s.get("outputTokens"))


def optimizer_client_errors(cloudwatch, model_id: str, start: datetime, end: datetime) -> int:
    """Sum InvocationClientErrors for the optimizer model over the job window.

    The optimizer runs with the caller's credentials, so an account that
    cannot invoke it fails here, and the job may still report SUCCESS.
    """
    response = cloudwatch.get_metric_statistics(
        Namespace="AWS/Bedrock",
        MetricName="InvocationClientErrors",
        Dimensions=[{"Name": "ModelId", "Value": model_id}],
        StartTime=start,
        EndTime=end,
        Period=300,
        Statistics=["Sum"],
    )
    return int(sum(point["Sum"] for point in response.get("Datapoints", [])))


def audit(original: str, optimized: str, original_samples: list[dict],
          optimizer_errors: int) -> list[str]:
    """Return the reasons not to trust the summary score. Empty means none found."""
    reasons: list[str] = []
    if not prompt_changed(original, optimized):
        reasons.append("returned prompt is identical to the original; any score change is noise")
    empty = empty_inferences(original_samples)
    if empty:
        reasons.append(f"{empty} of {len(original_samples)} original-side inferences were empty")
    if optimizer_errors:
        reasons.append(f"optimizer logged {optimizer_errors} client errors during the job")
    return reasons


if __name__ == "__main__":
    # The Haiku 4.5 row from the 2026-10-26 lab, reduced to its shape.
    samples = [{"outputTokens": 0}] * 12 + [{"outputTokens": 284}] * 48
    for reason in audit("same prompt", "same  prompt", samples, optimizer_errors=0):
        print("DO NOT TRUST:", reason)
