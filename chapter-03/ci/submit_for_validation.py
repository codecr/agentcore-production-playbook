#!/usr/bin/env python3
"""
submit_for_validation.py: Submit one Cedar or Dogwood policy to AgentCore's
policy validation (the Cedar analyzer) via the bedrock-agentcore-control
`create_policy` API.

2nd edition (October 2026):
  - *.cedar files go in definition={"cedar": {"statement": ...}};
    *.dogwood files (temporal policies, guardrail conditions) go in
    definition={"policy": {"statement": ...}}. cedarpy cannot parse Dogwood,
    so AWS is the only validator for those files.
  - create_policy is asynchronous: it returns 202 with status CREATING and
    the validation verdict arrives later as ACTIVE or CREATE_FAILED with
    statusReasons. This script now polls GetPolicy for the terminal status
    and exits 1 on CREATE_FAILED. Treating the 202 as success was a bug.
  - --enforcement-mode LOG_ONLY creates the policy in per-policy shadow mode
    (decision logged, not enforced) inside an ENFORCE engine.
  - Requires boto3 >= 1.43.100 (the "policy" definition member is newer than
    the "cedar" one). The author tested parameter shapes, not live calls.

Used by the "Validate against AWS automated reasoning" step in
ci/policy-validation.yml (see Chapter 3 Section 3.7). With --dry-run, no AWS
call is made; the script only prints what it would submit and exits 0. This
is what CI actually exercises today, since a real staging Policy Engine is
not available in every environment that runs this workflow.

Requires: boto3. Real (non-dry-run) submission requires AWS credentials with
bedrock-agentcore-control:CreatePolicy on the target policy engine.
"""

import argparse
import re
import sys
import time


# create_policy's `name` parameter must match r'[A-Za-z][A-Za-z0-9_]*' (per
# the service model — see the introspection snippet in the README). Policy
# filenames in this repo start with a zero-padded number ("04_tenant_..."),
# which the API would reject outright, so real submissions get a safe prefix.
_NAME_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')


def sanitize_policy_name(name: str) -> str:
    """Make a policy_name safe for create_policy's `name` field.

    create_policy requires names to start with a letter and contain only
    letters, digits, and underscores. Our policy files are numbered
    (e.g. "04_tenant_scoped_data_access"), so we prefix with "p_" and strip
    anything else that wouldn't be accepted.
    """
    candidate = name if _NAME_PATTERN.match(name) else f"p_{name}"
    candidate = re.sub(r'[^A-Za-z0-9_]', '_', candidate)
    if not _NAME_PATTERN.match(candidate):
        candidate = f"p_{candidate}"
    return candidate[:48]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Submit a Cedar policy for AWS automated reasoning validation."
    )
    parser.add_argument("--policy-file", required=True, help="Path to the .cedar file.")
    parser.add_argument("--policy-name", required=True, help="Human-readable policy name.")
    parser.add_argument("--engine-id", required=True, help="Target Policy Engine ID.")
    parser.add_argument(
        "--gateway-arn",
        required=True,
        help=(
            "Gateway ARN this policy is meant to protect. Not a direct "
            "create_policy parameter (the Policy Engine is what's bound to "
            "a Gateway); used here as a sanity check that the policy's "
            "resource clause references the same ARN before we submit it."
        ),
    )
    parser.add_argument(
        "--validation-mode",
        default="FAIL_ON_ANY_FINDINGS",
        choices=["FAIL_ON_ANY_FINDINGS", "IGNORE_ALL_FINDINGS"],
        help="validationMode passed to create_policy.",
    )
    parser.add_argument(
        "--enforcement-mode",
        default="ACTIVE",
        choices=["ACTIVE", "LOG_ONLY"],
        help="Per-policy enforcementMode passed to create_policy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be submitted and exit without calling AWS.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    with open(args.policy_file, encoding="utf-8") as f:
        statement = f.read()

    if args.gateway_arn not in statement:
        print(
            f"WARNING: {args.gateway_arn!r} was not found in {args.policy_file}. "
            "The policy may still contain an unsubstituted placeholder ARN "
            "(REGION/ACCOUNT/GATEWAY_ID) — this is only a sanity check, not "
            "a hard failure.",
            file=sys.stderr,
        )

    api_name = sanitize_policy_name(args.policy_name)
    definition_key = "policy" if args.policy_file.endswith(".dogwood") else "cedar"

    if args.dry_run:
        print(f"[dry-run] Would submit policy '{args.policy_name}' (as '{api_name}') "
              f"to engine '{args.engine_id}' for gateway '{args.gateway_arn}'")
        print(f"[dry-run] validationMode={args.validation_mode} "
              f"enforcementMode={args.enforcement_mode} definition={definition_key}")
        print(f"[dry-run] statement ({len(statement)} chars):")
        print(statement)
        return 0

    import boto3  # deferred: not needed for --dry-run

    client = boto3.client("bedrock-agentcore-control")
    response = client.create_policy(
        name=api_name,
        definition={definition_key: {"statement": statement}},
        validationMode=args.validation_mode,
        enforcementMode=args.enforcement_mode,
        policyEngineId=args.engine_id,
    )
    policy_id = response["policyId"]
    print(f"Submitted '{args.policy_name}' as '{api_name}' ({policy_id}); waiting for verdict")

    for _ in range(60):
        policy = client.get_policy(policyEngineId=args.engine_id, policyId=policy_id)
        status = policy["status"]
        if status == "ACTIVE":
            print(f"ACTIVE: '{api_name}' passed validation")
            return 0
        if status.endswith("_FAILED"):
            for reason in policy.get("statusReasons", []):
                print(f"  {reason}")
            print(f"{status}: '{api_name}' failed validation")
            return 1
        time.sleep(5)
    print(f"Timed out waiting for '{api_name}' (last status {status})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
