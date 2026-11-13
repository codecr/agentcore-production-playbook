"""
multi_log_group_query.py — Reconstruct one request across agents whose spans
live in different log groups.

Accompanies Chapter 12 (Section 12.7).

Since July 20, 2026, newly created AgentCore Runtime agents write spans to a
per-endpoint log group:
    /aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>   (stream: spans)
Agents created earlier keep the shared `aws/spans` group unless migrated with
UNIFIED_TRACES_DESTINATION_ENABLED=true. A fleet built across that date has
both layouts, so the Chapter 11 single-query pattern must name every group.

This module builds the list from what is deployed (never by hand), keeps only
groups that exist, always adds `aws/spans`, and runs one Logs Insights query
filtered on the business correlation id.

Usage:
    python observability/multi_log_group_query.py --dry-run
    python observability/multi_log_group_query.py req-41c9e2 --days 14

Status: boto3 calls NOT executed for the 2nd edition. The per-agent log group name
and the `spans` stream were confirmed live on 2026-11-11; attribute paths in the
query depend on how your spans serialize custom attributes, so check one span
in your account first.
"""

import argparse
import os
import time

LEGACY_SPANS_GROUP = "aws/spans"
CORRELATION_ATTRIBUTE = "acme.correlation_id"   # same name as chapter-11
LOGS_INSIGHTS_MAX_GROUPS = 50                    # check the current quota


def runtime_span_groups(runtimes: list[dict]) -> list[str]:
    """Map deployed runtimes and endpoints to their per-agent span groups."""
    groups = []
    for runtime in runtimes:
        for endpoint in runtime.get("endpoints", ["DEFAULT"]):
            groups.append(f"/aws/bedrock-agentcore/runtimes/{runtime['agentRuntimeId']}-{endpoint}")
    return groups


def discover_runtimes(region: str) -> list[dict]:
    import boto3
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    runtimes = []
    for page in control.get_paginator("list_agent_runtimes").paginate():
        for item in page.get("agentRuntimes", []):
            runtime_id = item["agentRuntimeId"]
            endpoints = [
                endpoint["name"]
                for endpoint_page in control.get_paginator("list_agent_runtime_endpoints").paginate(
                    agentRuntimeId=runtime_id)
                for endpoint in endpoint_page.get("runtimeEndpoints", [])
            ]
            runtimes.append({"agentRuntimeId": runtime_id, "endpoints": endpoints or ["DEFAULT"]})
    return runtimes


def existing_groups(candidates: list[str], region: str) -> list[str]:
    import boto3
    logs = boto3.client("logs", region_name=region)
    found = []
    for name in candidates:
        response = logs.describe_log_groups(logGroupNamePrefix=name, limit=1)
        if any(group["logGroupName"] == name for group in response.get("logGroups", [])):
            found.append(name)
    return found


def build_query(correlation_id: str) -> str:
    return (
        "fields @timestamp, @logStream, attributes.acme.agent_name as agent, "
        "attributes.acme.hop as hop, name, attributes.acme.claim_mismatch as mismatch\n"
        f"| filter attributes.{CORRELATION_ATTRIBUTE} = '{correlation_id}'\n"
        "| sort @timestamp asc\n"
        "| limit 1000"
    )


def run(correlation_id: str, days: int, region: str) -> list[dict]:
    import boto3
    groups = existing_groups(runtime_span_groups(discover_runtimes(region)), region)
    groups.append(LEGACY_SPANS_GROUP)
    if len(groups) > LOGS_INSIGHTS_MAX_GROUPS:
        raise SystemExit(f"{len(groups)} groups exceeds one query; split by agent or use a log group prefix")
    logs = boto3.client("logs", region_name=region)
    end = int(time.time())
    query_id = logs.start_query(
        logGroupNames=groups, startTime=end - days * 86400, endTime=end,
        queryString=build_query(correlation_id),
    )["queryId"]
    while True:
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            return result.get("results", [])
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("correlation_id", nargs="?", default="req-41c9e2")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        sample = [
            {"agentRuntimeId": "support_orchestrator-AbC123", "endpoints": ["prod"]},
            {"agentRuntimeId": "refunds_agent-DeF456", "endpoints": ["prod"]},
            {"agentRuntimeId": "notifications_agent-GhI789", "endpoints": ["prod"]},
        ]
        print("log groups:")
        for group in runtime_span_groups(sample) + [LEGACY_SPANS_GROUP]:
            print("  ", group)
        print("query:\n" + build_query(args.correlation_id))
        return

    for row in run(args.correlation_id, args.days, args.region):
        print({field["field"]: field["value"] for field in row})


if __name__ == "__main__":
    main()
