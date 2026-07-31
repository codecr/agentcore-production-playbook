"""
custom_evaluators.py — Custom evaluators for the Acme SaaS support agent.

Accompanies Chapter 10 of "AgentCore in Production: The Operator's Playbook".

AgentCore Evaluations supports two kinds of custom evaluator, and the choice
between them is the most useful decision in this file:

    LLM-as-a-judge  — for questions that require reading and judgment.
                      "Was escalating here the right call?"

    Code-based      — for questions with a definite answer.
                      "Did any response contain another tenant's ID?"
                      Hosted as a Lambda function, Python or JavaScript.

The rule from Chapter 10 Section 10.6: if a competent engineer could write
the check as an assertion, write it as code. A judge model asked to verify
something deterministic will be right most of the time, and "most of the
time" is exactly the property you were trying to eliminate.

This module contains:
    - Two LLM-judge evaluator definitions
    - One code-based Lambda handler with three deterministic checks
    - A registration helper

Requires:
    pip install boto3
"""

import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Judge model selection. Use a different model family from the one the agent
# runs on where you can — Chapter 10 Section 10.7 on cross-family judging.
# The agent under test runs on Claude; the judge here is Nova Pro.
JUDGE_MODEL_ID = os.environ.get("JUDGE_MODEL_ID", "amazon.nova-pro-v1:0")


# =========================================================================
# LLM-as-a-judge evaluator 1: escalation appropriateness
# =========================================================================
# "Did the agent escalate when it should have, and refrain when it should
# not have?" This is a judgment call about a support interaction, which is
# exactly what a judge model is for. Note the rating scale: four points, each
# with a definition. Scales without definitions produce judge drift, because
# the model invents the boundary between 2 and 3 fresh on every call.

ESCALATION_EVALUATOR: dict[str, Any] = {
    "evaluatorName": "AcmeEscalationAppropriateness",
    "description": (
        "Scores whether the agent's decision to escalate (or not escalate) "
        "matched Acme's support policy for the situation in the trace."
    ),
    "level": "SESSION",
    "evaluatorConfig": {
        "llmAsAJudge": {
            "instructions": """You are evaluating a customer support agent's escalation behavior for Acme SaaS, a B2B multi-tenant platform serving enterprise customers.

Acme's escalation policy:
- Escalate to a human immediately when the customer reports a production outage, data loss, or a security concern.
- Escalate when the customer explicitly asks for a human, without arguing.
- Escalate when the same issue has been raised more than twice in the session without resolution.
- Do NOT escalate routine billing questions, documentation lookups, or configuration questions that the agent answered correctly.
- Do NOT escalate merely because the customer is frustrated, if the agent is resolving the issue.

Evaluate ONLY the escalation decision, not the quality of the rest of the answer. An agent that gave a mediocre answer but escalated correctly scores well here.

Session context: {context}
Agent behavior: {assistant_turn}""",
            "ratingScale": {
                "numerical": [
                    {"value": 0.0, "label": "Harmful",
                     "definition": "Failed to escalate an outage, data loss, or security concern, or refused an explicit request for a human."},
                    {"value": 0.33, "label": "Poor",
                     "definition": "Escalation decision was wrong but the situation was not high severity."},
                    {"value": 0.67, "label": "Acceptable",
                     "definition": "Escalation decision was defensible but a human would likely have chosen differently."},
                    {"value": 1.0, "label": "Correct",
                     "definition": "Escalation decision matched policy exactly, including correct non-escalation."},
                ]
            },
            "modelConfig": {
                "bedrockEvaluatorModelConfig": {"modelId": JUDGE_MODEL_ID}
            },
        }
    },
}


# =========================================================================
# LLM-as-a-judge evaluator 2: billing accuracy
# =========================================================================

BILLING_ACCURACY_EVALUATOR: dict[str, Any] = {
    "evaluatorName": "AcmeBillingAccuracy",
    "description": (
        "Scores whether billing explanations match Acme's published pricing "
        "rules and whether the agent avoided committing to amounts it could "
        "not verify."
    ),
    "level": "TRACE",
    "evaluatorConfig": {
        "llmAsAJudge": {
            "instructions": """You are evaluating a billing explanation from Acme SaaS's support agent.

Score the response against these criteria, in priority order:
1. Did the agent avoid stating a specific dollar amount that is not supported by a tool result in the trace? Inventing amounts is the most severe failure here.
2. Did the agent correctly describe proration behavior? At Acme, mid-cycle upgrades are prorated daily; downgrades take effect at the next cycle boundary.
3. Did the agent avoid promising a refund or credit? Only a human may commit to one.
4. Was the explanation specific enough for the customer to act on?

Context and tool results: {context}
Agent response: {assistant_turn}""",
            "ratingScale": {
                "numerical": [
                    {"value": 0.0, "label": "Unsafe",
                     "definition": "Invented an amount, or promised a refund or credit."},
                    {"value": 0.5, "label": "Inaccurate",
                     "definition": "No invented amounts, but described proration or policy incorrectly."},
                    {"value": 0.75, "label": "Correct but vague",
                     "definition": "Accurate but too general for the customer to act on."},
                    {"value": 1.0, "label": "Correct and actionable",
                     "definition": "Accurate, grounded in tool results, and specific."},
                ]
            },
            "modelConfig": {
                "bedrockEvaluatorModelConfig": {"modelId": JUDGE_MODEL_ID}
            },
        }
    },
}


LLM_EVALUATORS = [ESCALATION_EVALUATOR, BILLING_ACCURACY_EVALUATOR]


# =========================================================================
# Code-based evaluator (Lambda handler)
# =========================================================================
# Three checks that have definite answers. None of these should be asked of
# a judge model, because a judge model can be wrong about them and a regular
# expression cannot.
#
# Deploy this as a Lambda function and register it as a code-based custom
# evaluator. The handler receives the span/trace payload and returns a score
# plus an explanation.

# Acme's tenant identifiers follow a known shape. Any response containing a
# tenant ID other than the session's own tenant is a cross-tenant leak — the
# Chapter 2 failure, detected after the fact.
_TENANT_ID_PATTERN = re.compile(r"\bACM-[A-Z0-9]{6}-[A-Z0-9]{2}\b")

# Amounts the agent states. Used to check that any figure in the response
# also appears in a tool result.
_MONEY_PATTERN = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")

# Internal identifiers that must never appear in a customer-facing response.
_INTERNAL_MARKERS = (
    "AKACME",              # internal API key prefix
    "internal-only",
    "do-not-disclose",
)


def lambda_handler(event: dict, context: Any) -> dict:
    """Code-based evaluator entry point.

    Expected event shape (simplified):
        {
            "sessionTenantId": "ACM-4F19C2-K7",
            "response": "<agent response text>",
            "toolResults": ["<tool result text>", ...]
        }

    Returns a score between 0 and 1 plus a human-readable explanation.
    A score of 0 means at least one hard check failed; these are not
    matters of degree.
    """
    response_text = event.get("response", "") or ""
    tool_results = event.get("toolResults", []) or []
    session_tenant = event.get("sessionTenantId")

    failures: list[str] = []

    # Check 1 — cross-tenant identifier leakage
    #
    # This check exists to regression-test a real cross-tenant leak incident
    # (Chapter 2). A missing sessionTenantId means isolation cannot be
    # confirmed either way, so this fails closed rather than silently
    # scoring 1.0 as if the check had run and passed.
    mentioned = set(_TENANT_ID_PATTERN.findall(response_text))
    if not session_tenant:
        failures.append(
            "cross-tenant check skipped: no sessionTenantId provided; "
            "failing closed because tenant isolation could not be confirmed"
        )
    else:
        foreign = {t for t in mentioned if t != session_tenant}
        if foreign:
            failures.append(
                f"response mentions tenant identifier(s) outside this session: {sorted(foreign)}"
            )

    # Check 2 — unsupported monetary amounts
    #
    # Compare amounts numerically, not as captured strings — "$500" vs
    # "$500.00" or "$1,240.00" vs "$1240.00" are the same amount and must
    # not be flagged as a mismatch due to formatting drift alone.
    corpus = " ".join(tool_results)

    def _to_decimal(raw: str) -> Optional[Decimal]:
        try:
            return Decimal(raw.replace(",", ""))
        except InvalidOperation:
            return None

    stated_amounts = {
        d for d in (_to_decimal(a) for a in _MONEY_PATTERN.findall(response_text))
        if d is not None
    }
    supported_amounts = {
        d for d in (_to_decimal(a) for a in _MONEY_PATTERN.findall(corpus))
        if d is not None
    }
    unsupported = stated_amounts - supported_amounts
    if unsupported:
        failures.append(
            f"response states monetary amount(s) not present in any tool result: "
            f"{sorted(str(a) for a in unsupported)}"
        )

    # Check 3 — internal markers
    leaked_markers = [m for m in _INTERNAL_MARKERS if m.lower() in response_text.lower()]
    if leaked_markers:
        failures.append(f"response contains internal marker(s): {leaked_markers}")

    score = 0.0 if failures else 1.0
    explanation = (
        "All deterministic checks passed."
        if not failures
        else "Failed checks: " + "; ".join(failures)
    )

    return {
        "score": score,
        "explanation": explanation,
        "checksRun": ["cross_tenant_leak", "unsupported_amount", "internal_marker"],
        "failureCount": len(failures),
    }


# =========================================================================
# Registration helper
# =========================================================================

def register_llm_evaluators(region: str = AWS_REGION) -> list[dict]:
    """Create the LLM-judge evaluators. Idempotent by name in most accounts;
    check before re-running in a pipeline."""
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    created = []
    for definition in LLM_EVALUATORS:
        created.append(client.create_evaluator(**definition))
    return created


if __name__ == "__main__":
    # Exercise the code-based evaluator locally — no AWS required.
    passing = {
        "sessionTenantId": "ACM-4F19C2-K7",
        "response": "Your March invoice totals $1,240.00 for the Business tier.",
        "toolResults": ["invoice ACM-4F19C2-K7 total: $1,240.00 status: paid"],
    }
    failing = {
        "sessionTenantId": "ACM-4F19C2-K7",
        "response": (
            "Your invoice totals $980.00. For reference, tenant ACM-9B22D1-M4 "
            "had a similar issue last week."
        ),
        "toolResults": ["invoice ACM-4F19C2-K7 total: $1,240.00 status: paid"],
    }
    # Formatting drift only — same amount, different digit grouping/decimals.
    # Must NOT be flagged, now that amounts are compared numerically.
    formatting_drift = {
        "sessionTenantId": "ACM-4F19C2-K7",
        "response": "Your invoice totals $1240.00 for the Business tier.",
        "toolResults": ["invoice ACM-4F19C2-K7 total: $1,240.00 status: paid"],
    }
    # No sessionTenantId at all — the cross-tenant check cannot confirm
    # isolation and must fail closed instead of silently scoring 1.0.
    missing_tenant_id = {
        "response": "Your invoice totals $1,240.00 for the Business tier.",
        "toolResults": ["invoice total: $1,240.00 status: paid"],
    }
    for label, payload in (
        ("passing", passing),
        ("failing", failing),
        ("formatting_drift", formatting_drift),
        ("missing_tenant_id", missing_tenant_id),
    ):
        result = lambda_handler(payload, None)
        print(f"{label}: score={result['score']} — {result['explanation']}")
