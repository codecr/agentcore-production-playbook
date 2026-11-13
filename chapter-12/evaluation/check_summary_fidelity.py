"""
check_summary_fidelity.py — The code-based assertion from Section 12.9.

"If a competent engineer could write the check as an assertion, write it as
an assertion" (Chapter 10). Whether the orchestrator upgraded a conditional
sentence into an approval is such a check: it runs on the outgoing A2A
payload, not on the model's prose quality.

Feed it the A2A message/send payloads the orchestrator emitted for a golden
case (captured from its trace or a test harness) and the case file.

    python evaluation/check_summary_fidelity.py            # runs the demo
"""

import json
import re
from pathlib import Path

CASES = Path(__file__).parent / "delegation_golden_cases.jsonl"


def load_case(case_id: str) -> dict:
    for line in CASES.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def violations(case: dict, outgoing_payloads: list[dict]) -> list[str]:
    rules = case.get("assert_outgoing_a2a", {})
    found = []
    for payload in outgoing_payloads:
        message = payload["params"]["message"]
        metadata = message.get("metadata", {})
        for key in rules.get("must_not_contain_metadata_keys", []):
            if key in metadata:
                found.append(f"{payload['id']}: metadata carries '{key}={metadata[key]}'")
        text = " ".join(part.get("text", "") for part in message.get("parts", []))
        for pattern in rules.get("must_not_match_text", []):
            if re.search(pattern, text):
                found.append(f"{payload['id']}: text matches {pattern!r}")
    return found


def _demo() -> None:
    case = load_case("mh-001-summary-fidelity")
    opener_payload = {
        "id": "a2a-77104",
        "params": {"message": {
            "parts": [{"kind": "text", "text": "Customer globex-corp disputes usage overage on "
                       "INV-2026-0918 ($3,840.00). Credit approved by account manager J. Ortiz "
                       "(see thread). Please issue full credit and confirm."}],
            "metadata": {"tenant_id": "globex-corp", "approved_by": "j.ortiz",
                         "correlation_id": "req-41c9e2"},
        }},
    }
    faithful_payload = {
        "id": "a2a-77105",
        "params": {"message": {
            "parts": [{"kind": "text", "text": "Customer globex-corp disputes usage overage on "
                       "INV-2026-0918 ($3,840.00) as a possible metering error. The customer "
                       "quotes an account manager saying he would look into a credit; no "
                       "approval is recorded. Please assess."}],
            "metadata": {"tenant_id": "globex-corp", "correlation_id": "req-41c9e3"},
        }},
    }
    for payload in (opener_payload, faithful_payload):
        result = violations(case, [payload])
        print(payload["id"], "FAIL" if result else "PASS", *result, sep="\n  ")


if __name__ == "__main__":
    _demo()
