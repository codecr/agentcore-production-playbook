"""
cross_agent_trace.py — Correlate a request as it crosses agent boundaries,
and audit what agents exist against what the catalog says.

Accompanies Chapter 11 of "AgentCore in Production: The Operator's Playbook".

Two problems, one module, because in practice they are the same problem
asked at two time scales.

    RIGHT NOW:  "A customer says their refund request went nowhere. Show me
                everything that happened across all three agents involved."

    QUARTERLY:  "How many agents are running in production, who owns each
                one, and which of them are not in the catalog?"

The first needs trace correlation across agent boundaries. The second needs
an inventory reconciliation between what is deployed and what is registered.
Both fail for the same underlying reason: nobody propagated an identifier.

Requires:
    pip install boto3
    Agent instrumented with OpenTelemetry / ADOT, traces in CloudWatch
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import boto3

logger = logging.getLogger("cross_agent")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# The header/attribute name carrying the business-level correlation id across
# agent boundaries. This is NOT the OpenTelemetry trace id: a trace id is
# per-request and is often reset when one agent invokes another through a
# queue, an event, or an HTTP boundary that drops context. The correlation id
# is chosen by you, survives everything, and is the field you will actually
# search on at 03:00.
CORRELATION_ATTRIBUTE = "acme.correlation_id"
TENANT_ATTRIBUTE = "acme.tenant_id"
AGENT_ATTRIBUTE = "acme.agent_name"


# =========================================================================
# Propagation
# =========================================================================

@dataclass
class RequestContext:
    """The identity of a customer request, carried across every agent hop.

    Attach these to every span. The cost is three string attributes. The
    benefit is that a single query returns the whole story instead of three
    partial ones that have to be joined by timestamp and hope.
    """
    correlation_id: str          # stable for the whole customer request
    tenant_id: str               # which customer
    origin: str                  # the entry point: "support-chat", "email-intake"
    hop: int = 0                 # incremented on each agent-to-agent call
    path: list[str] = field(default_factory=list)   # agent names, in order

    def next_hop(self, agent_name: str) -> "RequestContext":
        """Derive the context to pass to the next agent."""
        return RequestContext(
            correlation_id=self.correlation_id,
            tenant_id=self.tenant_id,
            origin=self.origin,
            hop=self.hop + 1,
            path=[*self.path, agent_name],
        )

    def as_span_attributes(self, agent_name: str) -> dict[str, Any]:
        return {
            CORRELATION_ATTRIBUTE: self.correlation_id,
            TENANT_ATTRIBUTE: self.tenant_id,
            AGENT_ATTRIBUTE: agent_name,
            "acme.origin": self.origin,
            "acme.hop": self.hop,
            "acme.path": " > ".join([*self.path, agent_name]),
        }

    def as_payload_header(self) -> dict[str, str]:
        """Serialize for transport in an invocation payload.

        Agent-to-agent calls do not reliably carry OpenTelemetry context.
        Putting the correlation fields in the payload is unglamorous and
        works across every transport: HTTP, SQS, EventBridge, direct invoke.
        """
        return {
            "x-acme-correlation-id": self.correlation_id,
            "x-acme-tenant-id": self.tenant_id,
            "x-acme-origin": self.origin,
            "x-acme-hop": str(self.hop),
            "x-acme-path": " > ".join(self.path),
        }

    @classmethod
    def from_payload_header(cls, headers: dict[str, str]) -> Optional["RequestContext"]:
        cid = headers.get("x-acme-correlation-id")
        if not cid:
            return None
        path = headers.get("x-acme-path", "")
        return cls(
            correlation_id=cid,
            tenant_id=headers.get("x-acme-tenant-id", "unknown"),
            origin=headers.get("x-acme-origin", "unknown"),
            hop=int(headers.get("x-acme-hop", "0")),
            path=[p.strip() for p in path.split(">") if p.strip()],
        )


def instrument_span(span, context: RequestContext, agent_name: str) -> None:
    """Attach correlation attributes to an OpenTelemetry span.

    Call this once per agent invocation, at the root span. Child spans
    inherit through the normal OTel context; the boundary between agents is
    where you have to do it by hand.
    """
    for key, value in context.as_span_attributes(agent_name).items():
        span.set_attribute(key, value)


# =========================================================================
# Reconstruction — the 03:00 query
# =========================================================================

def reconstruct_request(
    correlation_id: str,
    log_group: str = "/aws/bedrock-agentcore/traces",
    hours_back: int = 24,
    region: str = AWS_REGION,
) -> list[dict[str, Any]]:
    """Pull every span across every agent for one customer request.

    Uses CloudWatch Logs Insights against the trace log group. If your traces
    land in a different destination, the query shape is the same: filter on
    the correlation attribute, sort by timestamp, project the fields that
    tell the story.
    """
    client = boto3.client("logs", region_name=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)

    query = f"""
        fields @timestamp, {AGENT_ATTRIBUTE}, `acme.hop`, name,
               `gen_ai.operation.name`, `gen_ai.tool.name`, status, duration
        | filter {CORRELATION_ATTRIBUTE} = "{correlation_id}"
        | sort @timestamp asc
        | limit 500
    """

    start_response = client.start_query(
        logGroupName=log_group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
    )
    query_id = start_response["queryId"]

    while True:
        result = client.get_query_results(queryId=query_id)
        # All terminal states, not just the happy-path three: a query stuck
        # on "Timeout" or "Unknown" would otherwise poll forever.
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout", "Unknown"):
            break
        time.sleep(1)

    if result["status"] != "Complete":
        raise RuntimeError(f"Logs Insights query ended as {result['status']}")

    rows = []
    for row in result.get("results", []):
        rows.append({field["field"]: field["value"] for field in row})
    return rows


def render_timeline(rows: list[dict[str, Any]]) -> str:
    """Format reconstructed spans as a readable cross-agent timeline."""
    if not rows:
        return "No spans found. Either the correlation id is wrong, or it was "\
               "not propagated across the boundary — which is the more common answer."

    lines = ["", "Cross-agent timeline", "=" * 72]
    current_agent = None
    for row in rows:
        agent = row.get(AGENT_ATTRIBUTE, "unknown")
        if agent != current_agent:
            lines.append(f"\n[{agent}]")
            current_agent = agent
        ts = row.get("@timestamp", "")[:23]
        op = row.get("gen_ai.tool.name") or row.get("gen_ai.operation.name") or row.get("name", "")
        status = row.get("status", "")
        duration = row.get("duration", "")
        lines.append(f"  {ts}  {op:<34} {status:<8} {duration}")
    return "\n".join(lines)


# =========================================================================
# Inventory reconciliation — the quarterly query
# =========================================================================

def list_deployed_agents(region: str = AWS_REGION) -> list[dict[str, Any]]:
    """Every AgentCore runtime actually deployed in this account and Region."""
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    deployed = []
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for runtime in page.get("agentRuntimes", []):
            deployed.append({
                "name": runtime.get("agentRuntimeName"),
                "arn": runtime.get("agentRuntimeArn"),
                "status": runtime.get("status"),
                "lastUpdated": str(runtime.get("lastUpdatedAt", "")),
            })
    return deployed


def list_registered_agents(registry_id: str, region: str = AWS_REGION) -> list[dict[str, Any]]:
    """Every record in the catalog, regardless of status."""
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    registered = []
    paginator = client.get_paginator("list_registry_records")
    for page in paginator.paginate(registryId=registry_id):
        # The output key is `registryRecords`, not `records` — this is the
        # same key used by SearchRegistryRecords on the data plane. Reading
        # "records" here silently returns nothing on every page, which is
        # why reconcile() used to report every deployed agent as
        # unregistered: `registered` was always empty.
        for record in page.get("registryRecords", []):
            registered.append({
                "name": record.get("name"),
                "recordId": record.get("recordId"),
                "type": record.get("descriptorType"),
                "status": record.get("status"),
                "description": record.get("description", ""),
            })
    return registered


def reconcile(registry_id: str, region: str = AWS_REGION) -> dict[str, Any]:
    """Compare what is deployed against what is catalogued.

    Three buckets, and the first one is the reason this function exists:

      unregistered — running in production, absent from the catalog. These
                     are the agents nobody counted. Each one needs an owner
                     and a record, or a decommission date.
      stale        — catalogued but not deployed. Usually harmless, sometimes
                     a sign that a consumer is about to try invoking
                     something that no longer exists.
      matched      — both. The goal is for this list to be everything.
    """
    deployed = {a["name"]: a for a in list_deployed_agents(region)}
    registered = {r["name"]: r for r in list_registered_agents(registry_id, region)}

    unregistered = sorted(set(deployed) - set(registered))
    stale = sorted(set(registered) - set(deployed))
    matched = sorted(set(deployed) & set(registered))

    unowned = []
    for name in matched:
        description = registered[name].get("description", "")
        if "owner:" not in description.lower():
            unowned.append(name)

    return {
        "deployed_count": len(deployed),
        "registered_count": len(registered),
        "unregistered": unregistered,
        "stale": stale,
        "matched": matched,
        "matched_without_owner": unowned,
        "coverage": round(len(matched) / len(deployed), 3) if deployed else None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Demonstrate propagation locally — no AWS required.
    root = RequestContext(
        correlation_id="req-8f31c0a4",
        tenant_id="globex-corp",
        origin="support-chat",
    )
    print("Propagation across three agents:\n")
    ctx = root
    for agent in ("acme-support-agent", "acme-ticket-triage-tool", "acme-billing-mcp"):
        attrs = ctx.as_span_attributes(agent)
        print(f"  hop {attrs['acme.hop']}  {agent}")
        print(f"        path: {attrs['acme.path']}")
        ctx = ctx.next_hop(agent)

    print("\nPayload header handed to the next agent:")
    print(json.dumps(ctx.as_payload_header(), indent=2))

    print("\nRound-trip through a transport boundary:")
    restored = RequestContext.from_payload_header(ctx.as_payload_header())
    print(f"  correlation_id preserved: {restored.correlation_id == root.correlation_id}")
    print(f"  path preserved:           {' > '.join(restored.path)}")
