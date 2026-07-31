"""
validate_ar_claim.py — Validate claims against an Automated Reasoning policy
using the ApplyGuardrail API.

Accompanies Chapter 5 of "AgentCore in Production: The Operator's Playbook".

What this script does:
    Given a claim (a statement made by an agent), runs it through a Guardrail
    that has Automated Reasoning Checks configured. Returns the verdict —
    VALID, INVALID, SATISFIABLE, IMPOSSIBLE, TRANSLATION_AMBIGUOUS,
    TOO_COMPLEX, or NO_TRANSLATIONS — along with the formal justification.

How to use:
    1. In the AWS Bedrock console, create an Automated Reasoning policy.
       Upload `policies/acme-lending-mortgage-policy.md` (convert to PDF first
       if your console requires it). Review the extracted rules.

    2. Attach the AR policy to a Bedrock Guardrail (Automated Reasoning
       Checks safeguard). Note the Guardrail ID and version.

    3. Run this script with a claim:

       export GUARDRAIL_ID=<your-guardrail-id>
       export GUARDRAIL_VERSION=DRAFT
       python validate_ar_claim.py "The applicant qualifies for the Premium tier."

    4. Or validate the entire test corpus:

       python validate_ar_claim.py --corpus tests/test-claims.jsonl

Requires:
    pip install boto3
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import boto3


# =========================================================================
# Configuration
# =========================================================================

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)


# =========================================================================
# Result types
# =========================================================================

# The seven outcomes Automated Reasoning Checks can return.
# See Section 5.3 of the book for the meaning of each.
VALID_OUTCOMES = {
    "VALID",
    "INVALID",
    "SATISFIABLE",
    "IMPOSSIBLE",
    "TRANSLATION_AMBIGUOUS",
    "TOO_COMPLEX",
    "NO_TRANSLATIONS",
}

# ApplyGuardrail's `finding` object is a discriminated union: exactly one
# of these keys is present. Maps the wire key to our display constant.
FINDING_TYPE_MAP = {
    "valid": "VALID",
    "invalid": "INVALID",
    "satisfiable": "SATISFIABLE",
    "impossible": "IMPOSSIBLE",
    "translationAmbiguous": "TRANSLATION_AMBIGUOUS",
    "tooComplex": "TOO_COMPLEX",
    "noTranslations": "NO_TRANSLATIONS",
}

# Severity order (worst first) for picking an aggregate result when a
# single validation call returns multiple findings. Mirrors the
# aggregation approach AWS documents in "Determine the aggregate result"
# (integrate-automated-reasoning-checks.html).
SEVERITY_ORDER = {
    "TRANSLATION_AMBIGUOUS": 0,
    "TOO_COMPLEX": 0,
    "IMPOSSIBLE": 1,
    "INVALID": 2,
    "SATISFIABLE": 3,
    "VALID": 4,
    "NO_TRANSLATIONS": 5,
}


def _finding_union_key(finding: dict) -> Optional[str]:
    """Return which of the seven union keys is present on this finding."""
    for key in FINDING_TYPE_MAP:
        if key in finding:
            return key
    return None


def _explain_finding(union_key: str, data: dict) -> tuple:
    """Build a human-readable explanation and the untranslated-claim texts
    for one finding, given which union variant fired.

    The nested shape differs by variant:
      - valid / invalid / satisfiable / impossible carry a `translation`
        object (premises, claims, untranslatedPremises, untranslatedClaims).
        `valid` additionally carries `supportingRules`; `invalid` and
        `impossible` carry `contradictingRules`; `satisfiable` carries
        `claimsTrueScenario` and `claimsFalseScenario`.
      - translationAmbiguous carries `options` (each with its own nested
        `translations` list) and `differenceScenarios` instead of a single
        top-level `translation`.
      - tooComplex and noTranslations carry no fields at all.
    """
    untranslated = []

    if union_key in ("valid", "invalid", "satisfiable", "impossible"):
        translation = data.get("translation", {})
        for uc in translation.get("untranslatedClaims", []):
            untranslated.append(uc.get("text", ""))

        if union_key == "valid":
            ids = [r.get("identifier", "") for r in data.get("supportingRules", [])]
            explanation = f"Supported by rule(s): {', '.join(ids)}" if ids else None
        elif union_key in ("invalid", "impossible"):
            ids = [r.get("identifier", "") for r in data.get("contradictingRules", [])]
            explanation = f"Contradicts rule(s): {', '.join(ids)}" if ids else None
        else:  # satisfiable
            true_stmts = [
                s.get("naturalLanguage", "")
                for s in data.get("claimsTrueScenario", {}).get("statements", [])
            ]
            false_stmts = [
                s.get("naturalLanguage", "")
                for s in data.get("claimsFalseScenario", {}).get("statements", [])
            ]
            parts = []
            if true_stmts:
                parts.append("True when: " + "; ".join(true_stmts))
            if false_stmts:
                parts.append("False when: " + "; ".join(false_stmts))
            explanation = " | ".join(parts) if parts else None

    elif union_key == "translationAmbiguous":
        for option in data.get("options", []):
            for translation in option.get("translations", []):
                for uc in translation.get("untranslatedClaims", []):
                    untranslated.append(uc.get("text", ""))
        n_options = len(data.get("options", []))
        explanation = (
            f"{n_options} competing interpretation(s) found; see "
            "differenceScenarios in raw_response for details."
            if n_options else None
        )

    else:  # tooComplex, noTranslations -- the union member has no fields
        explanation = None

    return explanation, untranslated


@dataclass
class ARResult:
    """Result of an Automated Reasoning validation."""
    finding_type: str           # One of VALID_OUTCOMES
    claim: str                  # The claim that was validated
    premises: Optional[str] = None
    explanation: Optional[str] = None
    untranslated_claims: list = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)

    @property
    def is_certain(self) -> bool:
        """True if AR gave a deterministic answer (VALID or INVALID).

        Per Section 5.3 of the book, these are the only two outcomes that
        production code should treat as conclusive. Everything else requires
        human review.
        """
        return self.finding_type in {"VALID", "INVALID"}

    @property
    def requires_human_review(self) -> bool:
        """True if AR's output is ambiguous and a human must adjudicate."""
        return self.finding_type in {
            "SATISFIABLE",
            "IMPOSSIBLE",
            "TRANSLATION_AMBIGUOUS",
            "TOO_COMPLEX",
            "NO_TRANSLATIONS",
        }


# =========================================================================
# Core validation function
# =========================================================================

def validate_claim(
    claim: str,
    premises: Optional[str] = None,
    guardrail_id: Optional[str] = None,
    guardrail_version: Optional[str] = None,
) -> ARResult:
    """Run a claim through Automated Reasoning Checks.

    Args:
        claim: The statement to verify against the policy.
        premises: Optional premises (facts the claim depends on). When
            premises are provided, AR validates the claim given those facts.
            When omitted, AR validates the claim against the policy alone.
        guardrail_id: Override the GUARDRAIL_ID env var.
        guardrail_version: Override the GUARDRAIL_VERSION env var.

    Returns:
        ARResult with the finding type, explanation, and raw response.

    Raises:
        ValueError: If neither GUARDRAIL_ID env var nor arg is set.
    """
    gid = guardrail_id or GUARDRAIL_ID
    gver = guardrail_version or GUARDRAIL_VERSION

    if not gid:
        raise ValueError(
            "GUARDRAIL_ID is not set. Configure via env var or argument."
        )

    # Build the content payload.
    #
    # ApplyGuardrail treats each content block as user-side or agent-side:
    #   "query"         -> user-side. Context AR draws premises from.
    #                      This is where `premises` goes.
    #   "guard_content" -> agent-side. The content AR validates against
    #                      the policy (the claim to check).
    #   unqualified     -> ALSO agent-side. Tagging is optional; by default
    #                      every content block is treated as a claim.
    #   "grounding_source" is for contextual grounding checks only. A block
    #                      carrying it is excluded from every other policy
    #                      evaluation, so AR never sees it.
    #
    # Unlike Converse/InvokeModel, ApplyGuardrail never appends a model
    # response for you, so the request must include at least one claim
    # block — either unqualified or explicitly "guard_content" — or the API
    # raises a ValidationException. This code tags the claim explicitly
    # rather than relying on the default, so the intent survives someone
    # later adding a "query" qualifier to the wrong block. See "Call
    # ApplyGuardrail with Automated Reasoning checks" in
    # docs.aws.amazon.com/bedrock/latest/userguide/
    # integrate-automated-reasoning-checks.html.
    content_blocks = []
    if premises:
        content_blocks.append({
            "text": {
                "text": premises,
                "qualifiers": ["query"],
            }
        })
    content_blocks.append({
        "text": {
            "text": claim,
            "qualifiers": ["guard_content"],
        }
    })

    response = bedrock_runtime.apply_guardrail(
        guardrailIdentifier=gid,
        guardrailVersion=gver,
        source="OUTPUT",     # AR validates outputs, not user inputs
        content=content_blocks,
    )

    # Extract the AR finding from the response.
    #
    # assessments[].automatedReasoningPolicy.findings[] is a list of
    # discriminated unions -- each finding has exactly one of the keys in
    # FINDING_TYPE_MAP, and the useful fields (explanation material,
    # untranslated claims) are nested inside that key, not on the finding
    # itself. See _finding_union_key / _explain_finding above.
    #
    # A single call can return multiple findings; report the worst
    # (most severe) one, per AWS's own aggregation guidance.
    finding_type = "NO_TRANSLATIONS"
    explanation = None
    untranslated = []
    worst_severity = float("inf")

    for assessment in response.get("assessments", []):
        ar_policy = assessment.get("automatedReasoningPolicy")
        if not ar_policy:
            continue

        for finding in ar_policy.get("findings", []):
            union_key = _finding_union_key(finding)
            if union_key is None:
                continue  # Unrecognized shape; skip defensively.

            this_type = FINDING_TYPE_MAP[union_key]
            this_explanation, this_untranslated = _explain_finding(
                union_key, finding[union_key]
            )
            untranslated.extend(this_untranslated)

            severity = SEVERITY_ORDER.get(this_type, 0)
            if severity < worst_severity:
                worst_severity = severity
                finding_type = this_type
                explanation = this_explanation

    return ARResult(
        finding_type=finding_type,
        claim=claim,
        premises=premises,
        explanation=explanation,
        untranslated_claims=untranslated,
        raw_response=response,
    )


# =========================================================================
# Corpus runner
# =========================================================================

def run_corpus(corpus_path: str) -> dict:
    """Run an entire JSONL test corpus through validation.

    Each line of the JSONL file should have the shape:
        {
            "id": "test-001",
            "expected": "VALID",
            "premises": "...",
            "claim": "..."
        }

    Returns a summary dict with counts of matching/mismatching results.
    """
    matches = 0
    mismatches = []
    errors = []

    with open(corpus_path) as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                tc = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"Line {line_num}: JSON parse error — {exc}")
                continue

            test_id = tc.get("id", f"line-{line_num}")
            expected = tc.get("expected")
            premises = tc.get("premises")
            claim = tc.get("claim")

            if not (expected and claim):
                errors.append(f"{test_id}: missing 'expected' or 'claim'")
                continue

            try:
                result = validate_claim(claim, premises)
            except Exception as exc:
                errors.append(f"{test_id}: validation error — {exc}")
                continue

            if result.finding_type == expected:
                matches += 1
                print(f"  ✓ {test_id} → {result.finding_type}")
            else:
                mismatches.append({
                    "id": test_id,
                    "expected": expected,
                    "actual": result.finding_type,
                    "claim": claim[:80],
                })
                print(
                    f"  ✗ {test_id} → expected {expected}, got "
                    f"{result.finding_type}"
                )

    return {
        "total": matches + len(mismatches) + len(errors),
        "matches": matches,
        "mismatches": mismatches,
        "errors": errors,
    }


# =========================================================================
# CLI
# =========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a claim against an Automated Reasoning policy."
    )
    parser.add_argument(
        "claim",
        nargs="?",
        help="The claim to validate (omit when using --corpus).",
    )
    parser.add_argument(
        "--premises",
        help="Optional premises that ground the claim.",
    )
    parser.add_argument(
        "--corpus",
        help="Path to a JSONL test corpus to run instead of a single claim.",
    )
    args = parser.parse_args()

    if args.corpus:
        print(f"Running corpus: {args.corpus}")
        summary = run_corpus(args.corpus)
        print()
        print(f"Total cases: {summary['total']}")
        print(f"Matches:     {summary['matches']}")
        print(f"Mismatches:  {len(summary['mismatches'])}")
        print(f"Errors:      {len(summary['errors'])}")
        if summary["mismatches"] or summary["errors"]:
            return 1
        return 0

    if not args.claim:
        parser.error("Provide either a claim or --corpus.")

    try:
        result = validate_claim(args.claim, args.premises)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Finding:       {result.finding_type}")
    print(f"Certain?       {'yes' if result.is_certain else 'no — review'}")
    if result.explanation:
        print(f"Explanation:   {result.explanation}")
    if result.untranslated_claims:
        print()
        print("Untranslated claims (could not be converted to formal logic):")
        for uc in result.untranslated_claims:
            print(f"  - {uc}")

    return 0 if result.is_certain else 2


if __name__ == "__main__":
    sys.exit(main())
