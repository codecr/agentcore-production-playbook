"""
online_config.py — Manage AgentCore Evaluations online configurations.

Accompanies Chapter 10 of "AgentCore in Production: The Operator's Playbook".

Online evaluation samples live production traffic, scores it with the
evaluators you select, and publishes the results to CloudWatch. It is the
mechanism that would have caught the Friday-night model swap in Section 10.1
sometime around Saturday morning instead of Monday.

This module wraps the control-plane calls with the defaults the chapter
argues for:

    - Start with three evaluators, not thirteen (Section 10.4)
    - 10% sampling for medium traffic, dialed by volume (Section 10.5)
    - Filters added only after a week of unfiltered baseline data
    - Every configuration is code, versioned, and reviewable

API NOTE: AgentCore Evaluations reached GA on 2026-03-31. The real control
plane reads production traffic from CloudWatch Logs only — there is no
"point this at a runtime ARN directly" field. `CreateOnlineEvaluationConfig`
requires `dataSourceConfig.cloudWatchLogs.{logGroupNames,serviceNames}`,
`rule.samplingConfig.samplingPercentage`, `evaluationExecutionRoleArn`, and
`enableOnCreate`. This module still accepts an agent runtime ARN as the
primary input, for convenience, and derives the CloudWatch log group and
service name from it using AgentCore Runtime's documented naming convention
(see `cloudwatch_source_for_runtime` below) — pass `log_group_names` /
`service_names` explicitly if your runtime does not follow that convention.

Requires:
    pip install boto3
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import boto3

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Built-in evaluators are global resources and are referenced either by the
# short ID or by the global ARN form. The short ID is what you pass in the
# API; the ARN is what shows up in IAM policies.
BUILTIN_ARN_TEMPLATE = "arn:aws:bedrock-agentcore:::evaluator/{evaluator_id}"


# =========================================================================
# The three evaluators to start with
# =========================================================================
# Chapter 10 Section 10.4 argues for starting with exactly these three and
# adding more only when a specific question demands it:
#
#   Helpfulness           — is the answer useful to the person who asked?
#   ToolSelectionAccuracy — did the agent reach for the right tool?
#   GoalSuccessRate       — did the session accomplish what it set out to do?
#
# Between them they cover the response, the trajectory, and the outcome.
# Thirteen evaluators on day one produces thirteen dashboards nobody reads.

STARTER_EVALUATORS: list[str] = [
    "Builtin.Helpfulness",
    "Builtin.ToolSelectionAccuracy",
    "Builtin.GoalSuccessRate",
]

# Add these once the starter three have a stable baseline and you have a
# specific question they cannot answer.
SAFETY_EVALUATORS: list[str] = [
    "Builtin.Harmfulness",
    "Builtin.Stereotyping",
]

RAG_EVALUATORS: list[str] = [
    "Builtin.Faithfulness",
    "Builtin.ContextRelevance",
]


# =========================================================================
# Sampling guidance
# =========================================================================

def recommended_sampling_rate(sessions_per_day: int) -> float:
    """Pick a sampling percentage for a given traffic volume.

    The trade-off is cost against statistical usefulness. Too low and a
    real regression hides inside the noise for days; too high and you are
    paying judge inference on traffic that tells you nothing new.

    The shape of this function is the chapter's rule of thumb: sample enough
    to get on the order of a few hundred scored sessions per day, and cap
    the rate so very high volume does not run away with the budget.
    """
    if sessions_per_day <= 0:
        return 100.0
    target_scored_per_day = 300
    rate = (target_scored_per_day / sessions_per_day) * 100
    return round(min(100.0, max(1.0, rate)), 2)


# =========================================================================
# CloudWatch source derivation
# =========================================================================
# AgentCore Evaluations discovers agent traces from CloudWatch Logs — there
# is no field anywhere in the control-plane API that takes a runtime ARN
# directly. AgentCore Runtime logs and identifies itself predictably enough
# that the two CloudWatch inputs can be derived from the runtime ARN:
#
#   log group name : /aws/bedrock-agentcore/runtimes/{agentRuntimeId}-{endpoint}
#   service name   : {agentRuntimeName}.{endpoint}
#
# agentRuntimeId is the resource id in the ARN (the part after "runtime/")
# and, per the AWS CLI reference for CreateAgentRuntime/ListAgentRuntimes,
# always has the shape "{agentRuntimeName}-{10 alphanumeric characters}" — a
# name, a hyphen, then a fixed 10-character generated suffix. Splitting on
# that fixed-width suffix is what lets the service name be recovered from
# the ARN even when the name itself contains hyphens.
#
# If a runtime does not follow this convention (a pre-GA runtime, a manually
# assigned id, etc.) pass log_group_names / service_names explicitly instead
# of relying on derivation.

_RUNTIME_ID_PATTERN = re.compile(r"^(?P<name>.+)-(?P<suffix>[A-Za-z0-9]{10})$")


def cloudwatch_source_for_runtime(
    agent_runtime_arn: str, endpoint_name: str = "DEFAULT"
) -> tuple[str, str]:
    """Derive (log_group_name, service_name) for an AgentCore Runtime ARN.

    Raises ValueError if the runtime id does not match the documented
    "{name}-{10 alphanumeric chars}" shape — in that case the caller should
    supply log_group_names / service_names directly.
    """
    resource_id = agent_runtime_arn.rsplit("/", 1)[-1]
    log_group_name = f"/aws/bedrock-agentcore/runtimes/{resource_id}-{endpoint_name}"

    match = _RUNTIME_ID_PATTERN.match(resource_id)
    if not match:
        raise ValueError(
            f"cannot derive a service name from runtime id '{resource_id}' — "
            "it does not match the '{name}-{10 alphanumeric chars}' convention; "
            "pass service_names explicitly instead"
        )
    service_name = f"{match.group('name')}.{endpoint_name}"
    return log_group_name, service_name


# =========================================================================
# Configuration model
# =========================================================================

@dataclass
class OnlineEvaluationConfig:
    """A declarative description of one online evaluation configuration."""
    name: str
    agent_runtime_arn: str
    evaluation_execution_role_arn: str
    enable_on_create: bool = True
    endpoint_name: str = "DEFAULT"
    evaluators: list[str] = field(default_factory=lambda: list(STARTER_EVALUATORS))
    sampling_percentage: float = 10.0
    log_group_names: Optional[list[str]] = None
    service_names: Optional[list[str]] = None
    filters: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        # Only derive what the caller did not supply explicitly.
        if not self.log_group_names or not self.service_names:
            derived_log_group, derived_service = cloudwatch_source_for_runtime(
                self.agent_runtime_arn, self.endpoint_name
            )
            if not self.log_group_names:
                self.log_group_names = [derived_log_group]
            if not self.service_names:
                self.service_names = [derived_service]

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 0.01 <= self.sampling_percentage <= 100.0:
            problems.append("sampling_percentage must be between 0.01 and 100")
        if not self.evaluators:
            problems.append("at least one evaluator is required")
        if len(self.filters) > 5:
            problems.append("at most 5 filters are supported")
        if len(self.evaluators) > 5:
            problems.append(
                "more than 5 evaluators on a new configuration is usually a "
                "mistake — see Chapter 10 Section 10.4 on starting with three"
            )
        if not self.log_group_names or not (1 <= len(self.log_group_names) <= 5):
            problems.append("log_group_names must have between 1 and 5 entries")
        if not self.service_names or len(self.service_names) != 1:
            problems.append("service_names must have exactly 1 entry")
        if not self.evaluation_execution_role_arn:
            problems.append("evaluation_execution_role_arn is required")
        return problems

    def to_request(self) -> dict[str, Any]:
        rule: dict[str, Any] = {
            "samplingConfig": {"samplingPercentage": self.sampling_percentage},
        }
        if self.filters:
            rule["filters"] = self.filters

        request: dict[str, Any] = {
            "onlineEvaluationConfigName": self.name,
            "description": self.description or f"Online evaluation for {self.name}",
            "rule": rule,
            "dataSourceConfig": {
                "cloudWatchLogs": {
                    "logGroupNames": self.log_group_names,
                    "serviceNames": self.service_names,
                }
            },
            "evaluators": [{"evaluatorId": e} for e in self.evaluators],
            "evaluationExecutionRoleArn": self.evaluation_execution_role_arn,
            "enableOnCreate": self.enable_on_create,
        }
        return request


# =========================================================================
# Client wrapper
# =========================================================================

class EvaluationConfigClient:
    """Control-plane operations for online evaluation configurations."""

    def __init__(self, region: str = AWS_REGION):
        self.region = region
        self._client = boto3.client("bedrock-agentcore-control", region_name=region)

    def create(self, config: OnlineEvaluationConfig) -> dict:
        problems = config.validate()
        if problems:
            raise ValueError(
                f"Configuration '{config.name}' is invalid:\n  "
                + "\n  ".join(problems)
            )
        logger.info(
            "Creating online evaluation '%s' at %.2f%% sampling with %d evaluators",
            config.name, config.sampling_percentage, len(config.evaluators),
        )
        return self._client.create_online_evaluation_config(**config.to_request())

    def list(self) -> list[dict]:
        results: list[dict] = []
        paginator = self._client.get_paginator("list_online_evaluation_configs")
        for page in paginator.paginate():
            results.extend(page.get("onlineEvaluationConfigs", []))
        return results

    def disable(self, online_evaluation_config_id: str) -> dict:
        """Stop an online evaluation without deleting its history.

        Use this when a configuration is producing noise you have not had
        time to tune. Deleting loses the trend line, and the trend line is
        the whole point.
        """
        return self._client.update_online_evaluation_config(
            onlineEvaluationConfigId=online_evaluation_config_id,
            executionStatus="DISABLED",
        )


# =========================================================================
# The Acme SaaS reference configuration
# =========================================================================

def acme_support_config(
    agent_runtime_arn: str,
    evaluation_execution_role_arn: str,
    enable_on_create: bool = True,
    sessions_per_day: int = 3000,
) -> OnlineEvaluationConfig:
    """The configuration the Platform Team should have had on the Friday."""
    return OnlineEvaluationConfig(
        name="acme-support-online-eval",
        description=(
            "Continuous quality monitoring for the Acme SaaS customer support "
            "agent. Starter evaluator set; revisit after four weeks of baseline."
        ),
        agent_runtime_arn=agent_runtime_arn,
        evaluation_execution_role_arn=evaluation_execution_role_arn,
        enable_on_create=enable_on_create,
        evaluators=list(STARTER_EVALUATORS),
        sampling_percentage=recommended_sampling_rate(sessions_per_day),
        # No filters for the first week. Filters applied before you know what
        # normal looks like will hide the very traffic you needed to see.
        filters=[],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo = acme_support_config(
        agent_runtime_arn=(
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:"
            "runtime/acme-support-a1b2c3d4e5"
        ),
        evaluation_execution_role_arn=(
            "arn:aws:iam::111122223333:role/acme-support-eval-role"
        ),
        sessions_per_day=3000,
    )
    print(json.dumps(demo.to_request(), indent=2))
    print()
    for volume in (200, 3_000, 50_000, 500_000):
        print(f"{volume:>8} sessions/day -> {recommended_sampling_rate(volume):>6.2f}% sampling")
