"""
session_lifecycle.py — Production helpers for managing AgentCore session
lifecycles with Session Storage.

Accompanies Chapter 6 of "AgentCore in Production: The Operator's Playbook".

Three patterns this module supports:

1. Safe stop with a flush grace period — stops a session and waits out a
   fixed grace period before returning. There is no API to poll whether the
   filesystem flush to durable storage has actually completed (confirmed
   against the real `bedrock-agentcore` service model — see
   safe_stop_session()'s docstring for exactly what is and isn't
   guaranteed). This reduces, but does not eliminate, the race where a
   client stops a session and immediately tries to resume it before the
   data has finished replicating.

2. Orphan detection — approximates which sessions have not been invoked in
   a configurable threshold (default 24 hours) by mining the runtime's own
   CloudWatch logs. There is no native API to list a runtime's sessions;
   see find_orphan_sessions()'s docstring for the approximation and its
   limits.

3. Session ID generation — produces a >=33 character session ID that's
   deterministic from a user identity + a meaningful key, using a
   length-prefixed encoding so the two components can't collide across
   different (user, workspace) pairs. Avoids the hardcoded-ID anti-pattern
   from Chapter 6 Section 6.7.

Requires:
    pip install boto3
"""

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# =========================================================================
# Session ID generation
# =========================================================================

MIN_SESSION_ID_LENGTH = 33   # Validated client-side by AgentCore
MAX_SESSION_ID_LENGTH = 256  # runtimeSessionId max length in the service model


def generate_session_id(
    user_identifier: str,
    workspace_key: str,
    prefix: str = "session",
) -> str:
    """Generate a deterministic session ID >= 33 characters.

    Args:
        user_identifier: A stable identifier for the user or tenant. E.g.,
            "globex-corp/user-001" or "tenant-acme-internal".
        workspace_key: A label for what this session is doing. E.g.,
            "runbook-quarterly-2026q2" or "data-export-customer-42".
        prefix: A short prefix for readability in logs.

    Returns:
        A session ID of at least 33 characters, deterministic for the given
        (user_identifier, workspace_key) pair.

    Why deterministic: the same user resuming the same workspace gets the
    same session ID, which means the same persistent filesystem. Different
    users or different workspaces get different IDs and isolated filesystems.

    Collision safety: the two components are length-prefixed before being
    joined, not just concatenated with a "::" separator. A plain
    f"{user_identifier}::{workspace_key}" join is ambiguous whenever "::"
    appears inside either field — generate_session_id("tenantA::admin",
    "export") and generate_session_id("tenantA", "admin::export") would
    hash the identical string "tenantA::admin::export" and collide,
    silently pointing two different (user, workspace) pairs at the same
    persistent filesystem. Encoding each component's length before it
    (f"{len(a)}:{a}::{len(b)}:{b}") makes the pair unambiguously
    recoverable regardless of what characters appear inside a or b.
    """
    encoded = (
        f"{len(user_identifier)}:{user_identifier}"
        f"::{len(workspace_key)}:{workspace_key}"
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    session_id = f"{prefix}-{digest[:48]}"
    assert MIN_SESSION_ID_LENGTH <= len(session_id) <= MAX_SESSION_ID_LENGTH
    return session_id


# =========================================================================
# Safe stop with flush wait
# =========================================================================

@dataclass
class StopResult:
    session_id: str
    runtime_arn: str
    stopped_at: datetime
    flush_seconds_waited: float
    flush_confirmed: bool


def safe_stop_session(
    runtime_arn: str,
    session_id: str,
    region: str,
    flush_wait_seconds: float = 15.0,
) -> StopResult:
    """Stop a session and wait out a flush grace period before returning.

    IMPORTANT — there is no API to confirm the filesystem flush completed.
    AgentCore Runtime does not expose an operation to poll a session's
    disposition after StopRuntimeSession is called. Confirmed against the
    real `bedrock-agentcore` service model (boto3 1.43.10): the only
    session-lifecycle operation is `StopRuntimeSession`, and its response
    shape is just `{"runtimeSessionId": ..., "statusCode": ...}` — no status
    enum, no flush signal. There is no `get_runtime_session_status` or
    equivalent anywhere in the service model.

    AWS's own guidance is to call `StopRuntimeSession` and wait before
    resuming the session, because the call succeeding is the only signal
    available. This function formalizes that pattern:

        1. Call `StopRuntimeSession`. A 2xx `statusCode` in the response, or
           a `ResourceNotFoundException` (session already gone), both count
           as "the stop was accepted."
        2. Sleep `flush_wait_seconds` as a documented safety margin.

    What `flush_confirmed=True` actually means: the stop call was accepted
    and the grace period elapsed. It does NOT mean AWS confirmed the
    underlying filesystem replication finished — no such confirmation
    exists today. If a workload can't tolerate any residual risk of a lost
    write, pad `flush_wait_seconds` generously; there is nothing further to
    poll.

    Args:
        runtime_arn: The runtime ARN (full, with auto-appended suffix).
        session_id: The session to stop.
        region: AWS region.
        flush_wait_seconds: Fixed grace period to wait after the stop call
            returns, before treating the flush as "probably done." 15
            seconds matches the value the author used in tested,
            production-adjacent Session Storage stop/resume cycles; pad
            this for workloads with larger workspaces.

    Returns:
        StopResult. `flush_confirmed=False` only when `StopRuntimeSession`
        itself failed for a reason other than the session already being
        gone — callers should treat that as "the stop did not happen," not
        as a flush-timing problem.
    """
    client = boto3.client("bedrock-agentcore", region_name=region)
    start = time.time()

    try:
        response = client.stop_runtime_session(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            # Session was already stopped or never existed — nothing to flush.
            return StopResult(
                session_id=session_id,
                runtime_arn=runtime_arn,
                stopped_at=datetime.now(timezone.utc),
                flush_seconds_waited=0.0,
                flush_confirmed=True,
            )
        raise

    status_code = response.get("statusCode", 200)
    if not (200 <= status_code < 300):
        return StopResult(
            session_id=session_id,
            runtime_arn=runtime_arn,
            stopped_at=datetime.now(timezone.utc),
            flush_seconds_waited=0.0,
            flush_confirmed=False,
        )

    # No API exists to poll for flush completion (see docstring above).
    # This fixed wait is the best available approximation, not a guarantee.
    time.sleep(flush_wait_seconds)

    return StopResult(
        session_id=session_id,
        runtime_arn=runtime_arn,
        stopped_at=datetime.now(timezone.utc),
        flush_seconds_waited=time.time() - start,
        flush_confirmed=True,
    )


# =========================================================================
# Orphan session detection
# =========================================================================
#
# There is no native API to list a runtime's sessions. Confirmed against
# every operation in both the `bedrock-agentcore` and
# `bedrock-agentcore-control` service models (boto3 1.43.10, ~130
# operations total): no ListAgentRuntimeSessions, no equivalent under any
# other name. A `ListSessions` operation does exist, but it belongs to
# AgentCore Memory (it requires memoryId + actorId) — it lists memory
# actor sessions, not AgentCore Runtime sessions, and cannot be repurposed
# for this. This is a known, tracked gap in the service as of this
# writing (see the "No API to list or force-terminate active runtime
# sessions" issue on the bedrock-agentcore-starter-toolkit repo).
#
# There IS a native metric for how many sessions are alive right now:
# `ActiveSessionCount` in the `AWS/Bedrock-AgentCore` namespace (note the
# hyphen), published once per minute with a `Service` dimension of
# AgentCore.Runtime, AgentCore.CodeInterpreter or AgentCore.Browser. Use it
# for capacity alarms and quota headroom. It does not help here, because a
# gauge of "how many" cannot tell you "which ones, and how long idle" —
# that is what this function approximates.
#
# The approximation below mines the runtime's own CloudWatch logs the same
# way chapter-11/observability/cross_agent_trace.py reconstructs
# cross-agent traces: a Logs Insights query, grouped by session, using the
# most recent timestamp seen per session as a proxy for "last invoked."
# This is NOT a session inventory:
#   - It only sees sessions that logged something in the scanned window.
#   - It depends on the runtime's automatic OTEL instrumentation stamping
#     a session-identifying attribute on each span record. The field name
#     is `attributes.session.id` — `session.id` nested under `attributes`,
#     not a bare `session.id`. That is the field AWS's own on-demand
#     evaluation walkthrough filters on ("Download span-logs from
#     CloudWatch" in the AgentCore developer guide):
#
#         | filter ispresent(scope.name) and ispresent(attributes.session.id)
#         | filter attributes.session.id = "<session-id>"
#
#     `ispresent(scope.name)` is what separates span records from the
#     agent's own stdout, which lands in the same log group.

SESSION_ID_LOG_FIELD = "attributes.session.id"


@dataclass
class SessionInfo:
    runtime_arn: str
    session_id: str
    last_invocation_at: Optional[datetime]
    age_hours: Optional[float]


def _runtime_log_group(runtime_arn: str, endpoint_name: str = "DEFAULT") -> str:
    """Derive the runtime's log group name from its ARN.

        /aws/bedrock-agentcore/runtimes/<agentRuntimeId>-<endpoint_name>

    `<agentRuntimeId>` is the ARN segment after "runtime/", not the AWS
    account id. There is no trailing path segment: `runtime-logs` appears in
    the *log stream* name, and `otel-rt-logs` is a separate log group for
    ADOT structured logs — neither belongs on the group that carries spans.
    Confirmed against the AgentCore developer guide ("View observability
    data", Traces and Spans) and the CDK `applicationLogGroup` property,
    which documents the same path.

    Span destination caveat: newly created agents in supported Regions write
    spans to the `spans` log stream of this group, but agents created before
    a Region gained that support write to the shared `aws/spans` log group
    instead (controlled by the `UNIFIED_TRACES_DESTINATION_ENABLED`
    environment variable). If this function's group yields nothing, pass
    log_group="aws/spans" to find_orphan_sessions().
    """
    agent_runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return f"/aws/bedrock-agentcore/runtimes/{agent_runtime_id}-{endpoint_name}"


def find_orphan_sessions(
    runtime_arn: str,
    region: str,
    idle_threshold_hours: float = 24.0,
    log_group: Optional[str] = None,
    lookback_hours: float = 24.0 * 14,
) -> list[SessionInfo]:
    """Approximate which sessions have gone idle, via CloudWatch Logs.

    See the module comment above this function for why this is a
    CloudWatch Logs Insights query rather than a native session-listing
    API call — no such API exists today. Useful for cost controls and for
    surfacing the "orphan session" anti-pattern from Chapter 6 Section
    6.7, with the caveat that this is a best-effort, log-derived view, not
    an authoritative session inventory.

    Args:
        runtime_arn: The runtime to scan.
        region: AWS region.
        idle_threshold_hours: How long a session must be idle (by its most
            recently logged timestamp) to count as orphaned.
        log_group: Override the auto-derived OTEL log group name. Use this
            if your runtime logs elsewhere or under a custom destination.
        lookback_hours: How far back the Logs Insights query scans.
            Defaults to 14 days — the same window AWS uses to auto-reset
            an idle session's filesystem, since a session idle longer than
            that no longer holds billable state worth flagging. Sessions
            with no log activity inside this window are invisible to this
            function, whether they're truly gone or just outside the scan.

    Returns:
        List of SessionInfo records for sessions exceeding the threshold,
        sorted oldest-first.
    """
    logs_client = boto3.client("logs", region_name=region)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=lookback_hours)
    threshold = timedelta(hours=idle_threshold_hours)
    group = log_group or _runtime_log_group(runtime_arn)

    query = f"""
        fields @timestamp, `{SESSION_ID_LOG_FIELD}`
        | filter ispresent(`{SESSION_ID_LOG_FIELD}`)
        | stats latest(@timestamp) as last_seen, count(*) as event_count
              by `{SESSION_ID_LOG_FIELD}`
        | sort last_seen asc
    """

    start_response = logs_client.start_query(
        logGroupName=group,
        startTime=int(start.timestamp()),
        endTime=int(now.timestamp()),
        queryString=query,
    )
    query_id = start_response["queryId"]

    while True:
        result = logs_client.get_query_results(queryId=query_id)
        # All terminal states, not just the happy-path three. CloudWatch's
        # QueryStatus enum also includes "Timeout" and "Unknown"; breaking
        # only on Complete/Failed/Cancelled polls forever when a query ends
        # in either of those. Same handling as
        # chapter-11/observability/cross_agent_trace.py.
        if result["status"] in (
            "Complete", "Failed", "Cancelled", "Timeout", "Unknown"
        ):
            break
        time.sleep(1)

    if result["status"] != "Complete":
        raise RuntimeError(f"Logs Insights query ended as {result['status']}")

    orphans: list[SessionInfo] = []
    for row in result.get("results", []):
        fields = {f["field"]: f["value"] for f in row}
        session_id = fields.get(SESSION_ID_LOG_FIELD, "")
        last_seen_raw = fields.get("last_seen")
        if not session_id or not last_seen_raw:
            continue

        try:
            # CloudWatch Logs Insights renders @timestamp as
            # "YYYY-MM-DD HH:MM:SS.mmm". Millisecond-level rounding here
            # doesn't matter at the hour-scale thresholds this function
            # compares against.
            last_invoked = datetime.strptime(
                last_seen_raw, "%Y-%m-%d %H:%M:%S.%f"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        age = now - last_invoked
        if age >= threshold:
            orphans.append(SessionInfo(
                runtime_arn=runtime_arn,
                session_id=session_id,
                last_invocation_at=last_invoked,
                age_hours=age.total_seconds() / 3600.0,
            ))

    orphans.sort(key=lambda s: s.age_hours or 0.0, reverse=True)
    return orphans


# =========================================================================
# CLI smoke test
# =========================================================================

if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Session lifecycle helpers.")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate-id", help="Generate a deterministic session ID.")
    gen.add_argument("--user", required=True)
    gen.add_argument("--workspace", required=True)

    stop = sub.add_parser("safe-stop", help="Stop a session and wait out the flush grace period.")
    stop.add_argument("--runtime-arn", required=True)
    stop.add_argument("--session-id", required=True)
    stop.add_argument(
        "--max-wait", type=float, default=15.0,
        help="Fixed flush grace period (seconds) to wait after StopRuntimeSession "
             "returns. Not a poll timeout — there is no API to poll for flush "
             "completion; see safe_stop_session()'s docstring.",
    )

    orph = sub.add_parser("find-orphans", help="Approximate orphaned sessions via CloudWatch Logs.")
    orph.add_argument("--runtime-arn", required=True)
    orph.add_argument("--threshold-hours", type=float, default=24.0)
    orph.add_argument(
        "--log-group", default=None,
        help="Override the auto-derived OTEL log group name for this runtime.",
    )

    args = parser.parse_args()

    if args.cmd == "generate-id":
        sid = generate_session_id(args.user, args.workspace)
        print(sid)
    elif args.cmd == "safe-stop":
        result = safe_stop_session(
            args.runtime_arn, args.session_id, args.region, args.max_wait,
        )
        print(json.dumps({
            "session_id": result.session_id,
            "flush_seconds_waited": round(result.flush_seconds_waited, 2),
            "flush_confirmed": result.flush_confirmed,
        }, indent=2))
        sys.exit(0 if result.flush_confirmed else 2)
    elif args.cmd == "find-orphans":
        orphans = find_orphan_sessions(
            args.runtime_arn, args.region, args.threshold_hours,
            log_group=args.log_group,
        )
        for o in orphans:
            print(json.dumps({
                "session_id": o.session_id,
                "last_invocation_at": o.last_invocation_at.isoformat()
                    if o.last_invocation_at else None,
                "age_hours": round(o.age_hours, 1) if o.age_hours else None,
            }))
        print(f"\n{len(orphans)} orphan session(s) found", file=sys.stderr)
