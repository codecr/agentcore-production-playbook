#!/usr/bin/env python3
"""
submit_for_validation.py — Submit one Cedar policy to AgentCore's automated
reasoning validation via the bedrock-agentcore-control `create_policy` API.

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

    if args.dry_run:
        print(f"[dry-run] Would submit policy '{args.policy_name}' (as '{api_name}') "
              f"to engine '{args.engine_id}' for gateway '{args.gateway_arn}'")
        print(f"[dry-run] validationMode={args.validation_mode}")
        print(f"[dry-run] statement ({len(statement)} chars):")
        print(statement)
        return 0

    import boto3  # deferred: not needed for --dry-run

    client = boto3.client("bedrock-agentcore-control")
    response = client.create_policy(
        name=api_name,
        definition={"cedar": {"statement": statement}},
        validationMode=args.validation_mode,
        policyEngineId=args.engine_id,
    )
    print(f"Submitted '{args.policy_name}' as '{api_name}': {response}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
