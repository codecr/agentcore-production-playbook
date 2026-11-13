"""
publish_records.py — Registry lifecycle on the GA `agent-registry` namespace.

Accompanies Chapter 11 of "AgentCore in Production: The Operator's Playbook",
2nd edition (October 2026).

    create registry            (Administrator)   agent-registry-control
    create record  -> DRAFT    (Publisher)       agent-registry-control
    submit         -> PENDING  (Publisher)  -> EventBridge -> Curator notified
    approve/reject -> APPROVED (Curator)    -> discoverable
                   -> REJECTED              -> kept as history, not searchable
    search                     (Consumer)        agent-registry (data plane)

What changed from the first edition (preview, `bedrock-agentcore` namespace,
shut down October 30, 2026):
    * clients are `agent-registry-control` and `agent-registry`
    * auto-approval is `approvalConfiguration={"autoApprovalRules": [...]}`
      (empty list = manual approval), not `{"autoApproval": False}`
    * search is `SearchDiscoverableRegistryRecords`; it takes exactly one
      registry id and returns at most 20 results
    * creating a registry or a record is asynchronous (CREATING first)
    * ListRegistryRecords filters are a structured `filters` list

The persona boundaries are IAM boundaries. Running `approve` with Publisher
credentials should return AccessDeniedException; `prove-boundary` checks it.

Run against the GA namespace on 2026-11-10: create-registry, publish, submit, approve,
reject, and search. `prove-boundary` and personas.tf were not run. Parameters are also
validated offline against the botocore service model (repo/_verify).

Usage:
    python publish_records.py create-registry --name acme-agent-catalog
    python publish_records.py publish --registry-id <id>
    python publish_records.py submit  --registry-id <id> --all
    python publish_records.py approve --registry-id <id> --record-id <rid> \
        --reason "Meets naming and security standards; owner and contact documented."
    python publish_records.py search  --registry-id <id> --query "explain an invoice"
    python publish_records.py prove-boundary --registry-id <id> --record-id <rid>
"""

import argparse
import json
import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from registry.record_shapes import ACME_CATALOG, to_create_request  # noqa: E402

logger = logging.getLogger("registry")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Regions where AWS Agent Registry is available, as of October 2026 (GA did
# not add any). Check the AgentCore regions table before assuming.
REGISTRY_REGIONS = {
    "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2", "ap-northeast-1"
}

MIN_REASON_LENGTH = 20


def _control(region: str = AWS_REGION):
    if region not in REGISTRY_REGIONS:
        logger.warning("%s was not an Agent Registry Region as of October 2026", region)
    return boto3.client("agent-registry-control", region_name=region)


def _data(region: str = AWS_REGION):
    return boto3.client("agent-registry", region_name=region)


def _id_from_arn(arn: str) -> str | None:
    return arn.rsplit("/", 1)[-1] if arn else None


# =========================================================================
# Administrator
# =========================================================================

def create_registry(name: str, description: str, region: str) -> dict:
    """Create a registry with manual approval. Administrator persona.

    `autoApprovalRules: []` means every submission waits for a Curator.
    ["APPROVE_ALL"] is the GA equivalent of the preview's autoApproval=True;
    keep it for sandbox registries only.

    The response carries only `registryArn`. The registry id used by every
    other call is its trailing segment (12-16 alphanumerics); both the ARN
    and the id are accepted wherever `registryId` is expected.
    """
    client = _control(region)
    response = client.create_registry(
        name=name,
        description=description,
        approvalConfiguration={"autoApprovalRules": []},
    )
    registry_arn = response.get("registryArn", "")
    print(json.dumps({
        "registryArn": registry_arn,
        "registryId": _id_from_arn(registry_arn),
        "autoApprovalRules": [],
        "note": "creation is asynchronous; wait for status READY (get_registry)",
    }, indent=2))
    return response


# =========================================================================
# Publisher
# =========================================================================

def publish_catalog(registry_id: str, region: str) -> list[dict]:
    """Create every record in the example catalog. Publisher persona.

    Records start in CREATING (asynchronous) and settle in DRAFT. The
    response carries `recordArn` and `status`; the record id is the
    trailing ARN segment (12 alphanumerics).
    """
    client = _control(region)
    created = []
    for record in ACME_CATALOG:
        request = to_create_request(record, registry_id)
        try:
            response = client.create_registry_record(**request)
            record_arn = response.get("recordArn", "")
            created.append({
                "name": record.name,
                "recordArn": record_arn,
                "recordId": _id_from_arn(record_arn),
                "status": response.get("status", "CREATING"),
            })
            print(f"  created  {record.name:<28} {_id_from_arn(record_arn)}")
        except ClientError as exc:
            print(f"  FAILED   {record.name:<28} {exc}")
    return created


def submit_record(registry_id: str, record_id: str, region: str) -> dict:
    """DRAFT -> PENDING_APPROVAL. Publisher persona.

    The service emits the EventBridge event `Registry Record State changed to
    Pending Approval` (source `aws.agent-registry`); personas.tf routes it to
    the Curator.
    """
    client = _control(region)
    response = client.submit_registry_record_for_approval(
        registryId=registry_id, recordId=record_id
    )
    print(f"  submitted {record_id} -> {response.get('status', 'PENDING_APPROVAL')}")
    return response


def list_drafts(registry_id: str, region: str) -> list[dict]:
    client = _control(region)
    drafts = []
    paginator = client.get_paginator("list_registry_records")
    for page in paginator.paginate(
        registryId=registry_id,
        filters=[{"name": "status", "values": ["DRAFT"]}],
    ):
        drafts.extend(page.get("registryRecords", []))
    return drafts


# =========================================================================
# Curator
# =========================================================================

def set_status(registry_id: str, record_id: str, status: str, reason: str,
               region: str) -> dict:
    """Transition a record's status. Curator persona.

    The service model marks both `status` (not `newStatus`) and
    `statusReason` as required, so botocore rejects a call without them
    before it leaves the machine (ParamValidationError). The model also
    allows an empty reason (min length 0, max 255): "required" guarantees
    the field exists, not that anyone wrote in it. This helper enforces a
    floor, as a local policy, because "ok" is not an audit trail.
    """
    if not reason or len(reason.strip()) < MIN_REASON_LENGTH:
        raise ValueError(
            "statusReason must be substantive. In six months someone will ask "
            "why this record was approved; 'ok' is not an answer."
        )
    if len(reason) > 255:
        raise ValueError("statusReason is limited to 255 characters by the service")
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
# Consumer
# =========================================================================

def search(registry_id: str, query: str, region: str, max_results: int = 10,
           record_type: str | None = None) -> list[dict]:
    """Search approved records. Consumer persona, data plane.

    Exactly one registry id per call; maxResults 1-20. Only APPROVED records
    are discoverable, so "rejected" and "does not exist" look identical here.
    """
    client = _data(region)
    params: dict = {
        "registryIds": [registry_id],
        "searchQuery": query,
        "maxResults": max(1, min(max_results, 20)),
    }
    if record_type:
        params["filters"] = {"recordType": {"$eq": record_type}}
    response = client.search_discoverable_registry_records(**params)
    results = response.get("registryRecords", [])
    if not results:
        print(f"  no matches for '{query}'")
        print("  (short queries behaved like keyword search in the 2026 lab — Section 11.6)")
    for r in results:
        print(f"  {r.get('name', '?'):<30} {r.get('recordType', '?'):<8} {r.get('recordId', '')}")
    return results


# =========================================================================
# Persona boundary demonstration
# =========================================================================

def prove_boundary(registry_id: str, record_id: str, region: str) -> bool:
    """Attempt an approval with the current credentials and report the result.

    Run it as a Publisher. The expected outcome is AccessDeniedException.
    """
    try:
        set_status(registry_id, record_id, "APPROVED",
                   "Boundary test — this call is expected to be denied for Publishers.",
                   region)
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
    parser = argparse.ArgumentParser(description="AWS Agent Registry lifecycle (GA).")
    parser.add_argument("command", choices=[
        "create-registry", "publish", "submit", "approve", "reject", "deprecate",
        "search", "prove-boundary",
    ])
    parser.add_argument("--registry-id")
    parser.add_argument("--record-id")
    parser.add_argument("--name", default="acme-agent-catalog")
    parser.add_argument("--description",
                        default="Acme SaaS private catalog of agents, tools, and skills.")
    parser.add_argument("--reason", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--record-type", choices=["AGENT", "MCP", "SKILL", "CUSTOM", "GATEWAY"])
    parser.add_argument("--all", action="store_true", help="Apply to every DRAFT record.")
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
            for rec in list_drafts(args.registry_id, args.region):
                submit_record(args.registry_id, rec["recordId"], args.region)
        else:
            if not args.record_id:
                parser.error("--record-id or --all is required")
            submit_record(args.registry_id, args.record_id, args.region)
    elif args.command in ("approve", "reject", "deprecate"):
        if not args.record_id:
            parser.error("--record-id is required")
        status = {"approve": "APPROVED", "reject": "REJECTED",
                  "deprecate": "DEPRECATED"}[args.command]
        set_status(args.registry_id, args.record_id, status, args.reason, args.region)
    elif args.command == "search":
        if not args.query:
            parser.error("--query is required")
        search(args.registry_id, args.query, args.region, record_type=args.record_type)
    elif args.command == "prove-boundary":
        if not args.record_id:
            parser.error("--record-id is required")
        prove_boundary(args.registry_id, args.record_id, args.region)
    return 0


if __name__ == "__main__":
    sys.exit(main())
