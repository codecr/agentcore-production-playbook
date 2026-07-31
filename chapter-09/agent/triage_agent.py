"""
triage_agent.py — The Acme SaaS ticket triage agent, before and after.

Accompanies Chapter 9 of "AgentCore in Production: The Operator's Playbook".

This file deliberately contains BOTH implementations of the same agent:

    TriageAgentBefore  — prompt-as-contract, ~130 lines of defensive parsing,
                         and a keyword fallback. This is the version that
                         mislabeled a P1 outage as P3 in Section 9.1.

    TriageAgentAfter   — schema-as-contract. One json.loads. No fallback,
                         because there is no parse failure to fall back from.

Keeping them side by side is the point. The diff between the two classes is
the chapter's argument in code.

Requires:
    pip install boto3
"""

import json
import logging
import os
import re
from typing import Any, Optional

import boto3

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from schemas.production_schemas import TRIAGE_SCHEMA, as_output_config  # noqa: E402


logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Structured Outputs requires a supported model. Nova and Titan are not
# supported as of mid-2026; Haiku 4.5 is the cheapest Anthropic model that is.
MODEL_ID_AFTER = os.environ.get(
    "TRIAGE_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

# The "before" version ran on a model without Structured Outputs support,
# which is precisely why it needed the defensive scaffolding.
MODEL_ID_BEFORE = os.environ.get("TRIAGE_MODEL_ID_LEGACY", "amazon.nova-lite-v1:0")

bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)


# =========================================================================
# BEFORE — prompt as contract
# =========================================================================

class TriageAgentBefore:
    """The version that asks the model to be consistent, then hopes.

    Three layers of scaffolding exist here purely to compensate for the fact
    that nothing guarantees the response shape:

        1. Format instructions in the system prompt (natural language).
        2. Defensive extraction and validation of the response.
        3. A keyword fallback when both of the above fail.

    Layer three is the dangerous one. It does not fail loudly; it produces a
    plausible answer that the rest of the system trusts.
    """

    SYSTEM_PROMPT = """You are a support ticket triage agent for Acme SaaS.

RESPONSE FORMAT — MANDATORY:
You must respond with ONLY a valid JSON object. No explanations, no
markdown fences, no text before or after the JSON.

REQUIRED STRUCTURE:
{
  "priority": "P1",
  "category": "api-error",
  "affected_tenant_id": "globex-corp",
  "summary": "Short summary here",
  "confidence": 0.9,
  "needs_human_review": false
}

IMPORTANT: Respond with ONLY the JSON. Do not add text before or after.
"""

    MAX_ATTEMPTS = 3

    def triage(self, ticket_text: str) -> dict[str, Any]:
        for attempt in range(self.MAX_ATTEMPTS):
            response = bedrock.converse(
                modelId=MODEL_ID_BEFORE,
                system=[{"text": self.SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": ticket_text}]}],
                inferenceConfig={"temperature": 0.0, "maxTokens": 1024},
            )
            raw = response["output"]["message"]["content"][0]["text"]

            extracted = self._extract_json_from_text(raw)
            if extracted is None:
                logger.warning("Attempt %d: no JSON found in response", attempt + 1)
                continue

            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError:
                logger.warning("Attempt %d: JSONDecodeError", attempt + 1)
                continue

            if not self._validate_structure(parsed):
                logger.warning("Attempt %d: structure validation failed", attempt + 1)
                continue

            return parsed

        # Every attempt failed. Fall back to keywords.
        #
        # This is the line that produced the incident in Chapter 9 Section 9.1.
        # It returns something shaped like a triage result, so the router
        # accepts it, and nobody is paged.
        logger.error("All %d attempts failed; using keyword fallback", self.MAX_ATTEMPTS)
        return self._create_fallback_analysis(ticket_text)

    @staticmethod
    def _extract_json_from_text(text: str) -> Optional[str]:
        """Find a JSON object inside whatever the model returned."""
        cleaned = text.strip()

        # Strip markdown fences if present.
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
        if fence:
            cleaned = fence.group(1).strip()

        if cleaned.startswith("{"):
            return cleaned

        # Model added a preamble. Find the outermost braces.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return cleaned[start:end + 1]
        return None

    @staticmethod
    def _validate_structure(parsed: dict) -> bool:
        """Check every field the downstream router depends on."""
        required = {
            "priority": str,
            "category": str,
            "summary": str,
            "confidence": (int, float),
            "needs_human_review": bool,
        }
        for field, expected_type in required.items():
            if field not in parsed:
                return False
            if not isinstance(parsed[field], expected_type):
                return False
        if parsed["priority"] not in {"P1", "P2", "P3", "P4"}:
            return False
        if not 0.0 <= float(parsed["confidence"]) <= 1.0:
            return False
        return True

    @staticmethod
    def _create_fallback_analysis(ticket_text: str) -> dict[str, Any]:
        """Keyword heuristic used when the model could not be parsed.

        Every value here is a guess. The `confidence` of 0.5 is not a
        measurement of anything; it is a number chosen so the result looks
        reasonable. The router treats it exactly like a real triage result.
        """
        lowered = ticket_text.lower()
        if any(k in lowered for k in ("outage", "down", "critical", "data loss")):
            priority = "P1"
        elif any(k in lowered for k in ("slow", "degraded", "error")):
            priority = "P2"
        else:
            priority = "P3"   # default bucket — where the outage ticket landed

        return {
            "priority": priority,
            "category": "other",
            "affected_tenant_id": None,
            "summary": ticket_text[:140],
            "confidence": 0.5,
            "needs_human_review": False,   # nobody gets paged
        }


# =========================================================================
# AFTER — schema as contract
# =========================================================================

class TriageAgentAfter:
    """The same agent with the contract moved from the prompt into the schema.

    What disappeared:
        - The format instructions in the system prompt
        - _extract_json_from_text
        - _validate_structure
        - _create_fallback_analysis
        - The retry loop

    What the prompt talks about now is only business behavior: what P1 means
    at Acme, when to escalate, how to treat ambiguous tenant references.
    """

    SYSTEM_PROMPT = """You are a support ticket triage agent for Acme SaaS.

Acme serves 200 enterprise customers on a multi-tenant platform. Triage
decisions route tickets to on-call queues, so severity accuracy matters
more than speed.

Severity guidance:
- P1 is reserved for production outages, data loss, or security exposure
  affecting one or more customers. When in doubt between P1 and P2 for an
  outage, choose P1 and let the on-call engineer downgrade.
- P2 is degraded service, or a full outage limited to a single non-critical
  workflow.
- P3 is a functional problem with a documented workaround.
- P4 is questions, requests, and cosmetic issues.

Tenant identification: only populate affected_tenant_id when the ticket
names the tenant explicitly. Never infer it from the reporter's email
domain or from context.

Escalation: set needs_human_review to true when the ticket is ambiguous,
mentions legal or regulatory exposure, or when your confidence is below 0.7.
"""

    def triage(self, ticket_text: str) -> dict[str, Any]:
        response = bedrock.converse(
            modelId=MODEL_ID_AFTER,
            system=[{"text": self.SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": ticket_text}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 1024},
            outputConfig=as_output_config(
                TRIAGE_SCHEMA,
                name="ticket_triage",
                description="Triage classification for an inbound support ticket.",
            ),
        )
        # No extraction. No validation. No fallback. The response either
        # conforms to the schema or the API call failed, and a failed API
        # call is a loud failure that pages someone.
        return json.loads(response["output"]["message"]["content"][0]["text"])


# =========================================================================
# The routing layer, unchanged between versions
# =========================================================================

def route_ticket(triage_result: dict[str, Any]) -> str:
    """Decide the destination queue for a triaged ticket.

    Note what this function assumes: that `priority` is one of four known
    values and that `needs_human_review` is a boolean. With the BEFORE agent
    those assumptions rested on a validation function and a fallback that
    could quietly violate them. With the AFTER agent they rest on the schema.
    """
    if triage_result["needs_human_review"]:
        return "queue:human-triage"
    if triage_result["confidence"] < 0.7:
        return "queue:human-triage"
    return {
        "P1": "queue:oncall-page",
        "P2": "queue:oncall-business-hours",
        "P3": "queue:support-tier-2",
        "P4": "queue:support-tier-1",
    }[triage_result["priority"]]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    sample = (
        "Our entire API is returning 503 for every request since about "
        "02:10 UTC. Globex Corp production. Nothing changed on our side. "
        "This is taking down our checkout flow."
    )

    print("=== AFTER (schema as contract) ===")
    result = TriageAgentAfter().triage(sample)
    print(json.dumps(result, indent=2))
    print(f"routed to: {route_ticket(result)}")
