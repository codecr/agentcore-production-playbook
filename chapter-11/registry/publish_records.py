"""
publish_records.py — Registry lifecycle: create, publish, submit, adjudicate.

Accompanies Chapter 11 of "AgentCore in Production: The Operator's Playbook".

This script walks the full lifecycle of an AWS Agent Registry record:

    create registry            (Administrator)
    create record  -> DRAFT    (Publisher)
    submit         -> PENDING  (Publisher)  -> EventBridge -> Curator notified
    approve/reject -> APPROVED (Curator)    -> discoverable
                   -> REJECTED              -> kept as history, not searchable

The persona boundaries are real IAM boundaries, not conventions. Running the
submit step with Publisher credentials works; running the approve step with
the same credentials returns AccessDeniedException. The `--prove-boundary`
flag demonstrates this deliberately, because a governance control nobody has
tested is a governance control nobody should trust.

Requirements:
    boto3 >= 1.42.87        (earlier SDKs lack the registry methods entirely)
    AWS CLI v2 >= 2.34.28   (only if you want to use the CLI; boto3 is ahead)

Usage:
    # As Administrator
    python publish_records.py create-registry --name acme-agent-catalog

    # As Publisher
    python publish_records.py publish --registry-id <id>
    python publish_records.py submit --registry-id <id> --all

    # As Curator
    python publish_records.py approve --registry-id <id> --record-id <rid> \
        --reason "Meets naming and security standards; owner and contact documented."

    # As Consumer
    python publish_records.py search --registry-id <id> --query "explain an invoice"
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from registry.record_shapes import ACME_CATALOG, to_create_request  # noqa: E402

logger = logging.getLogger("registry")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Preview Regions as of April 2026. Check before assuming.
PREVIEW_REGIONS = {
    "us-east-1", "us-west-2", "ap-southeast-2", "ap-northeast-1", "eu-west-1"
}


def _control(region: str = AWS_REGION):
    if region not in PREVIEW_REGIONS:
        logger.warning(
            "%s was not a Registry preview Region as of April 2026; "
            "expect endpoint errors if coverage has not expanded", region
        )
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _data(region: str = AWS_REGION):
    return boto3.client("bedrock-agentcore", region_name=region)


# =========================================================================
# Administrator: create the registry
# =========================================================================

def create_registry(name: str, description: str, region: str) -> dict:
    """Create a registry. Administrator persona.

    Auto-approval is available and should stay off in production: it turns
    the approval workflow into a formality, which is the same as not having
    one. The point of the Curator is that a second person looked.

    Two API details worth knowing:

      1. Auto-approval is a NESTED parameter, `approvalConfiguration=
         {"autoApproval": False}` — not the top-level `autoApprovalEnabled`
         it is easy to guess. botocore rejects the flat form with
         ParamValidationError before any network call.
      2. CreateRegistry's response has exactly one useful field:
         `registryArn`. There is no `registryId` in the output — the
         `registryId` used everywhere else in this file (and in the
         --registry-id CLI flag) is the trailing ARN segment, derived here.
    """
    client = _control(region)
    response = client.create_registry(
        name=name,
        description=description,
        # Explicitly disabled. See Chapter 11 Section 11.5.
        approvalConfiguration={"autoApproval": False},
    )
    registry_arn = response.get("registryArn", "")
    registry_id = registry_arn.rsplit("/", 1)[-1] if registry_arn else None
    print(json.dumps({
        "registryArn": registry_arn,
        "registryId": registry_id,  # derived from the ARN; not an API field
        "autoApproval": False,
    }, indent=2))
    return response


# =========================================================================
# Publisher: create records and submit them
# =========================================================================

def publish_catalog(registry_id: str, region: str) -> list[dict]:
    """Create every record in the example catalog as DRAFT. Publisher persona.

    CreateRegistryRecord's response, like CreateRegistry's, only carries an
    ARN (`recordArn` plus `status`) — no `recordId` field. Every downstream
    call in this file (submit, approve/reject) takes `recordId`, so we parse
    it from the trailing ARN segment
    (`.../registry/<registryId>/record/<recordId>`) once here rather than
    letting every caller silently receive `None`.

    The parsing is a convenience, not a necessity: `registryId` and
    `recordId` both accept an ARN as well as a bare ID, so passing
    `recordArn` straight through works too. Deriving the short id keeps the
    console, the CLI examples and the `--record-id` flag consistent with
    each other.
    """
    client = _control(region)
    created = []
    for record in ACME_CATALOG:
        request = to_create_request(record, registry_id)
        try:
            response = client.create_registry_record(**request)
            record_arn = response.get("recordArn", "")
            record_id = record_arn.rsplit("/", 1)[-1] if record_arn else None
            created.append({
                "name": record.name,
                "recordArn": record_arn,
                "recordId": record_id,  # derived from the ARN; not an API field
                "status": response.get("status", "DRAFT"),
            })
            print(f"  created  {record.name:<28} {record_id}")
        except ClientError as exc:
            print(f"  FAILED   {record.name:<28} {exc}")
    return created


def submit_record(registry_id: str, record_id: str, region: str) -> dict:
    """Move a record from DRAFT to PENDING_APPROVAL. Publisher persona.

    This is the call EventBridge listens for; the rule in personas.tf turns
    it into an SNS notification to the Curator.
    """
    client = _control(region)
    response = client.submit_registry_record_for_approval(
        registryId=registry_id, recordId=record_id
    )
    print(f"  submitted {record_id} -> {response.get('status', 'PENDING_APPROVAL')}")
    return response


# =========================================================================
# Curator: approve or reject
# =========================================================================

def set_status(
    registry_id: str,
    record_id: str,
    status: str,
    reason: str,
    region: str,
) -> dict:
    """Transition a record's status. Curator persona.

    API details worth knowing:

      1. The parameter is `status`, not `newStatus`.
      2. `registryId` and `recordId` each accept EITHER the ARN or the bare
         ID, so the ARN returned by CreateRegistry/CreateRegistryRecord can
         be passed straight through without parsing an ID out of it.
      3. `statusReason` is NOT marked required by the service model or the
         SDK reference — only `registryId`, `recordId` and `status` are. This
         function requires it anyway, as a local policy rather than an API
         constraint: every state change should leave a structured,
         attributable reason behind it. In six months someone will ask why
         this record was approved. Resist the urge to type "ok".
    """
    if not reason or len(reason.strip()) < 20:
        raise ValueError(
            "statusReason must be substantive. In six months someone will ask "
            "why this record was approved; 'ok' is not an answer."
        )
    client = _control(region)
    response = client.update_registry_record_status(
        registryId=registry_id,
        recordId=record_id,
        status=status,
        statusReason=reason,
    )
    print(f"  {status:<9} {record_id}  — {reason[:60]}")
    return response


# =========================================================================
# Consumer: search
# =========================================================================

def search(registry_id: str, query: str, region: str, max_results: int = 10) -> list[dict]:
    """Search approved records. Consumer persona (data plane).

    Only APPROVED records are returned. REJECTED and DEPRECATED records
    remain as history and are invisible here, which is the correct behavior
    and also means "I cannot find it" and "it was rejected" look identical
    to a consumer. Tell your Publishers that.
    """
    client = _data(region)
    response = client.search_registry_records(
        # `registryIds` is a list — SearchRegistryRecords can search across
        # more than one registry in a single call. Passing the singular
        # `registryId` string raises ParamValidationError (unknown parameter).
        registryIds=[registry_id],
        searchQuery=query,
        maxResults=max_results,
    )
    # The output key is `registryRecords` for both SearchRegistryRecords and
    # ListRegistryRecords — not `records`. `response.get("records", [])`
    # silently returns an empty list forever, since "records" is never a key
    # in the real response.
    results = response.get("registryRecords", [])
    if not results:
        print(f"  no matches for '{query}'")
        print("  (short queries behave like keyword search — see Section 11.6)")
    for r in results:
        print(f"  {r.get('name', '?'):<30} {r.get('descriptorType', '?'):<14} {r.get('recordId', '')}")
    return results


# =========================================================================
# Persona boundary demonstration
# =========================================================================

def prove_boundary(registry_id: str, record_id: str, region: str) -> bool:
    """Attempt an approval with the current credentials and report the result.

    Run this with Publisher credentials. The expected outcome is
    AccessDeniedException, and a governance control that has never been
    tested is a governance control nobody should trust.
    """
    try:
        set_status(
            registry_id, record_id, "APPROVED",
            "Boundary test — this call is expected to be denied for Publishers.",
            region,
        )
        print("  UNEXPECTED: the approval succeeded. Check the persona policies.")
        return False
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "AccessDeniedException":
            print("  expected: AccessDeniedException — persona boundary holds")
            return True
        print(f"  unexpected error: {code}")
        return False


# =========================================================================
# CLI
# =========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="AWS Agent Registry lifecycle.")
    parser.add_argument("command", choices=[
        "create-registry", "publish", "submit", "approve", "reject",
        "search", "prove-boundary",
    ])
    parser.add_argument("--registry-id")
    parser.add_argument("--record-id")
    parser.add_argument("--name", default="acme-agent-catalog")
    parser.add_argument("--description",
                        default="Acme SaaS private catalog of agents, tools, and skills.")
    parser.add_argument("--reason", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--all", action="store_true", help="Apply to every record.")
    parser.add_argument("--region", default=AWS_REGION)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "create-registry":
        create_registry(args.name, args.description, args.region)
        return 0

    if not args.registry_id:
        parser.error("--registry-id is required for this command")

    if args.command == "publish":
        publish_catalog(args.registry_id, args.region)
    elif args.command == "submit":
        if args.all:
            client = _control(args.region)
            listed = client.list_registry_records(registryId=args.registry_id)
            # ListRegistryRecords' output key is `registryRecords`, same as
            # SearchRegistryRecords — not `records`.
            for rec in listed.get("registryRecords", []):
                if rec.get("status") == "DRAFT":
                    submit_record(args.registry_id, rec["recordId"], args.region)
        else:
            if not args.record_id:
                parser.error("--record-id or --all is required")
            submit_record(args.registry_id, args.record_id, args.region)
    elif args.command in ("approve", "reject"):
        if not args.record_id:
            parser.error("--record-id is required")
        status = "APPROVED" if args.command == "approve" else "REJECTED"
        set_status(args.registry_id, args.record_id, status, args.reason, args.region)
    elif args.command == "search":
        if not args.query:
            parser.error("--query is required")
        search(args.registry_id, args.query, args.region)
    elif args.command == "prove-boundary":
        if not args.record_id:
            parser.error("--record-id is required")
        prove_boundary(args.registry_id, args.record_id, args.region)

    return 0


if __name__ == "__main__":
    sys.exit(main())
