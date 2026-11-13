"""
forget_actor.py - delete one person's data from an AgentCore Memory resource.

Accompanies Chapter 7, Section 7.8 ("Purging a person"), 2nd edition.

This module DELETES, but only with execute=True; the default counts what it
would delete. It does not decide WHAT to delete: the caller supplies the actor
ID(s) resolved from the identity provider (procedure step 1) and the person's
own namespaces to clear (step 4). Shared namespaces (step 5) never go in: a
prefix purge there would delete every user's tenant facts, so their matches are
reviewed and deleted record by record. Keep identity resolution out of here so
the code someone runs under pressure is the code that was tested.

Order matters: short-term events first, because events still in short-term
memory can be extracted again (StartMemoryExtractionJob reprocesses events
whose extraction failed earlier). Then long-term records.

A TTL is not a substitute: expired records are removed by a background
process that can take days and can still be returned by reads meanwhile.

Usage (dry run prints counts only):
    python forget_actor.py --memory-id acme_support_memory-AbCdEf1234 \
        --actor-id maria-sub-0001 --tenant-id globex-corp [--execute]

IAM: bedrock-agentcore:ListSessions, ListEvents, DeleteEvent,
ListMemoryRecords, BatchDeleteMemoryRecords on the memory ARN. Grant these to
the deletion pipeline's role only; batch operations are not governable by
Memory FGAC (Cedar), only by IAM.

NAMESPACES: `namespace` on ListMemoryRecords matches exactly; `namespacePath` matches by
prefix. Summaries and episodes live one level below the namespace root, in
.../sessions/{sessionId}/ and .../incidents/{actorId}/{sessionId}/, so the
deletion lists with namespacePath. With `namespace=` this function found
nothing under them and left them behind (live-tested 2026-11-11, CT-07-05).

STATUS: run against a live account on 2026-11-11 (console test CT-07-05).
"""

import argparse
import json
import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_helpers import paginate, user_namespace  # noqa: E402

BATCH_LIMIT = 100  # BatchDeleteMemoryRecords accepts at most 100 records


def forget_actor(client, memory_id, actor_id, namespaces, execute=False):
    deleted_events = 0
    for session in paginate(client.list_sessions, "sessionSummaries",
                            memoryId=memory_id, actorId=actor_id):
        for event in paginate(client.list_events, "events",
                              memoryId=memory_id, actorId=actor_id,
                              sessionId=session["sessionId"]):
            if execute:
                client.delete_event(memoryId=memory_id, actorId=actor_id,
                                    sessionId=session["sessionId"],
                                    eventId=event["eventId"])
            deleted_events += 1

    deleted_records, failed = 0, []
    for namespace in namespaces:
        ids = [r["memoryRecordId"] for r in paginate(
            client.list_memory_records, "memoryRecordSummaries",
            memoryId=memory_id, namespacePath=namespace)]
        for start in range(0, len(ids), BATCH_LIMIT):
            batch = ids[start:start + BATCH_LIMIT]
            if execute:
                result = client.batch_delete_memory_records(
                    memoryId=memory_id,
                    records=[{"memoryRecordId": rid} for rid in batch])
                failed += result.get("failedRecords", [])
            deleted_records += len(batch)
    return {"events": deleted_events, "records": deleted_records,
            "failed_records": failed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete. Without it, only count.")
    args = parser.parse_args()

    # Per-user namespaces only. Shared namespaces (tenant facts, tenant
    # reflections) need the manual search-and-review of procedure step 5.
    namespaces = [user_namespace(args.tenant_id, args.actor_id, kind)
                  for kind in ("preferences", "facts", "sessions")]
    namespaces.append(f"/tenant/{args.tenant_id}/incidents/{args.actor_id}/")

    client = boto3.client("bedrock-agentcore", region_name=args.region)
    report = forget_actor(client, args.memory_id, args.actor_id, namespaces,
                          execute=args.execute)
    report["mode"] = "executed" if args.execute else "dry-run"
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
