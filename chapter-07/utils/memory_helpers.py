"""
memory_helpers.py — Production helpers for AgentCore Memory.

Accompanies Chapter 7 of "AgentCore in Production: The Operator's Playbook".

This module wraps the AgentCore Memory APIs with four patterns that the
chapter describes as canonical:

  1. hydrate_session_context()  — load preferences and summaries at session start
  2. record_user_event()        — store a user turn to short-term memory
  3. record_assistant_event()   — store an assistant turn to short-term memory
  4. end_session_with_summary() — trigger summary generation on session close

Plus a thin MemoryClient wrapper that handles the control-plane/data-plane
split correctly: only data-plane calls happen here. Control-plane operations
(create_memory, configure strategies) belong in IaC, not runtime code.

Requires:
    pip install boto3
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# =========================================================================
# MemoryClient — thin wrapper around the data-plane boto3 client
# =========================================================================

@dataclass
class MemoryRecord:
    """A single record returned from retrieve_memory_records."""
    record_id: str
    namespace: str
    content: str
    score: float
    created_at: Optional[datetime] = None


class MemoryClient:
    """Wrapper for the runtime side of AgentCore Memory.

    Holds the memory ID and region. Exposes only the data-plane operations
    that production agents need: create_event, retrieve_memory_records,
    list_events, list_sessions. Does NOT expose create_memory or strategy
    configuration — those are IaC concerns.
    """

    def __init__(self, memory_id: str, region: str):
        self.memory_id = memory_id
        self.region = region
        # bedrock-agentcore = data plane (events, retrieval)
        # bedrock-agentcore-control = control plane (memory, strategies) — NOT used here
        self._client = boto3.client("bedrock-agentcore", region_name=region)

    def retrieve(
        self,
        namespace: str,
        query: str,
        max_results: int = 5,
    ) -> dict:
        """Semantic search against long-term memory in a namespace."""
        try:
            response = self._client.retrieve_memory_records(
                memoryId=self.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": query,
                    "topK": max_results,
                },
            )
            records = []
            for r in response.get("memoryRecordSummaries", []):
                records.append(MemoryRecord(
                    record_id=r.get("memoryRecordId", ""),
                    namespace=namespace,
                    content=r.get("content", {}).get("text", ""),
                    score=r.get("score", 0.0),
                    created_at=r.get("createdAt"),
                ))
            return {
                "namespace": namespace,
                "query": query,
                "records": [
                    {
                        "id": r.record_id,
                        "content": r.content,
                        "score": round(r.score, 3),
                    }
                    for r in records
                ],
                "count": len(records),
            }
        except ClientError as exc:
            return {
                "namespace": namespace,
                "query": query,
                "error": str(exc),
                "records": [],
                "count": 0,
            }

    def create_event(
        self,
        actor_id: str,
        session_id: str,
        role: str,
        content: str,
    ) -> dict:
        """Send a single event to short-term memory.

        Args:
            actor_id: Combined tenant/user identifier (e.g., "globex-corp/user-001").
            session_id: Stable session identifier across turns.
            role: One of USER, ASSISTANT, TOOL. Note: semantic and preference
                strategies process only USER and ASSISTANT roles.
            content: The text of the turn.
        """
        try:
            return self._client.create_event(
                memoryId=self.memory_id,
                actorId=actor_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=[{
                    "conversational": {
                        "role": role,
                        "content": {"text": content},
                    }
                }],
            )
        except ClientError as exc:
            return {"error": str(exc)}


# =========================================================================
# Namespace helpers — the real actorId-based scheme
# =========================================================================
# The real CreateMemory API only supports {actorId}, {sessionId}, and
# {memoryStrategyId} as namespace placeholders — there is no {tenantId} or
# {userId}. Every per-user namespace in memory/acme-support-memory.yaml
# keys off {actorId}, so the runtime's actor_id must be built the same way
# everywhere. actor_id_for() is that single source of truth.
#
# Tenant-wide facts (AcmeTenantFacts) have no per-tenant-only placeholder
# either, so this module reserves a sentinel actor per tenant —
# f"{tenant_id}/__tenant_facts__" — and never lets a real user_id collide
# with it (see the ValueError in actor_id_for()).

TENANT_FACTS_ACTOR_SUFFIX = "__tenant_facts__"


def actor_id_for(tenant_id: str, user_id: str) -> str:
    """Build the composite actor ID used to partition per-user memory.

    Mirrors the {actorId} the CFN template's per-user Namespaces
    (users/{actorId}/...) expect. Raises ValueError if user_id collides
    with the reserved tenant-facts sentinel — that collision would let a
    real user's events land in (or read from) the tenant-wide namespace.
    """
    if user_id == TENANT_FACTS_ACTOR_SUFFIX:
        raise ValueError(
            f"user_id {user_id!r} collides with the reserved tenant-facts "
            "sentinel and cannot be used as a real user identifier."
        )
    return f"{tenant_id}/{user_id}"


def tenant_facts_actor_id(tenant_id: str) -> str:
    """Reserved sentinel actor ID used for tenant-wide facts.

    Never a real user — used only to write/read the AcmeTenantFacts
    strategy, which has no {tenantId}-only namespace placeholder to key
    off instead.
    """
    return f"{tenant_id}/{TENANT_FACTS_ACTOR_SUFFIX}"


def tenant_facts_namespace(tenant_id: str) -> str:
    """Resolve the AcmeTenantFacts namespace for a given tenant.

    Mirrors the CFN template's "tenant-facts/{actorId}" pattern, with
    {actorId} substituted by this tenant's reserved sentinel actor.
    """
    return f"tenant-facts/{tenant_facts_actor_id(tenant_id)}"


# =========================================================================
# Pattern 1: hydrate_session_context()
# =========================================================================
# Called at the start of a new session. Loads what the agent needs to know
# about this user from prior interactions: stated preferences, recent
# session summaries, key facts.
#
# The "customer who felt forgotten" from Chapter 7 Section 7.1 is the
# scenario this prevents: without hydration, the agent has no idea that
# the user previously stated preferences. With hydration, those preferences
# are in the context window from turn one.

def hydrate_session_context(
    memory_client: MemoryClient,
    tenant_id: str,
    user_id: str,
) -> Optional[str]:
    """Build a context block for the agent at session start.

    Queries memory for: user preferences, recent session summaries, and
    key tenant facts. Formats them as a structured context block that the
    agent's system prompt can be prepended with.

    Returns None if no relevant memory exists (cold start — first session
    for this user). Returns a formatted string otherwise.
    """
    pieces: list[str] = []
    actor_id = actor_id_for(tenant_id, user_id)

    # User preferences — most important
    prefs_namespace = f"users/{actor_id}/preferences"
    prefs = memory_client.retrieve(
        namespace=prefs_namespace,
        query="user communication and format preferences",
        max_results=5,
    )
    if prefs["records"]:
        pieces.append("## User preferences from prior sessions:")
        for r in prefs["records"]:
            pieces.append(f"- {r['content']}")

    # Recent session summaries
    summaries_namespace = f"users/{actor_id}/sessions"
    summaries = memory_client.retrieve(
        namespace=summaries_namespace,
        query="recent support sessions and their outcomes",
        max_results=3,
    )
    if summaries["records"]:
        if pieces:
            pieces.append("")
        pieces.append("## Recent prior sessions:")
        for r in summaries["records"]:
            pieces.append(f"- {r['content']}")

    # Tenant facts that often matter — read from the sentinel-actor
    # namespace (AcmeTenantFacts), not the per-user namespace.
    facts_namespace = tenant_facts_namespace(tenant_id)
    facts = memory_client.retrieve(
        namespace=facts_namespace,
        query="tenant account billing tier contact",
        max_results=5,
    )
    if facts["records"]:
        if pieces:
            pieces.append("")
        pieces.append("## Tenant facts:")
        for r in facts["records"]:
            pieces.append(f"- {r['content']}")

    if not pieces:
        return None  # Cold start
    return "\n".join(pieces)


# =========================================================================
# Pattern 2: record_user_event()
# =========================================================================

def record_user_event(
    memory_client: MemoryClient,
    tenant_id: str,
    user_id: str,
    session_id: str,
    content: str,
) -> dict:
    """Record a user turn to short-term memory.

    Actor ID combines tenant and user so memory is properly partitioned
    in multi-tenant settings.
    """
    actor_id = actor_id_for(tenant_id, user_id)
    return memory_client.create_event(
        actor_id=actor_id,
        session_id=session_id,
        role="USER",
        content=content,
    )


# =========================================================================
# Pattern 3: record_assistant_event()
# =========================================================================

def record_assistant_event(
    memory_client: MemoryClient,
    tenant_id: str,
    user_id: str,
    session_id: str,
    content: str,
) -> dict:
    """Record an assistant turn to short-term memory."""
    actor_id = actor_id_for(tenant_id, user_id)
    return memory_client.create_event(
        actor_id=actor_id,
        session_id=session_id,
        role="ASSISTANT",
        content=content,
    )


# =========================================================================
# Pattern 4b: record_tenant_fact_event()
# =========================================================================
# Writes to the AcmeTenantFacts strategy via the reserved sentinel actor.
# There is no automatic path from a normal per-user conversation event
# into the tenant-wide namespace (no {tenantId}-only placeholder exists),
# so tenant-wide facts must be recorded explicitly through this function
# instead of relying on extraction from ordinary user/assistant turns.

def record_tenant_fact_event(
    memory_client: MemoryClient,
    tenant_id: str,
    content: str,
    session_id: str = "tenant-facts",
) -> dict:
    """Record a fact that applies to the whole tenant, not one user.

    Use this for durable, tenant-wide knowledge (e.g., "Globex Corp is on
    the Business tier", "Globex Corp switched to annual billing in
    March") that every user at the tenant should benefit from, as
    opposed to record_user_event/record_assistant_event, which are
    scoped to a single user via actor_id_for().

    session_id defaults to a fixed value because the sentinel actor
    doesn't represent a real conversation session — any stable value
    works here, since AcmeTenantFacts' namespace only keys off actorId.
    """
    return memory_client.create_event(
        actor_id=tenant_facts_actor_id(tenant_id),
        session_id=session_id,
        role="ASSISTANT",
        content=content,
    )


# =========================================================================
# Pattern 4: end_session_with_summary()
# =========================================================================
# When a session ends, mark it explicitly. AgentCore's asynchronous
# extraction will then generate a summary for the just-ended session.
# Without an explicit end signal, the system has to infer session
# boundaries from inactivity timeouts.

def end_session_with_summary(
    memory_client: MemoryClient,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> dict:
    """Record an explicit session-end event.

    The end event is what tells the extraction pipeline "this episode is
    complete — start consolidation and summary generation." For episodic
    memory in particular, episodes only generate after this signal.

    Note: The actual extraction is asynchronous; the summary is not
    available immediately after this call returns. It typically appears
    20-40 seconds later for summary strategies, and minutes-not-seconds
    for episodic.
    """
    actor_id = actor_id_for(tenant_id, user_id)
    # The convention is a final ASSISTANT message marking completion.
    # Some teams use a TOOL-role event with a sentinel payload instead.
    return memory_client.create_event(
        actor_id=actor_id,
        session_id=session_id,
        role="ASSISTANT",
        content="[SESSION_END] Conversation ended by user; ready for consolidation.",
    )


# =========================================================================
# Smoke test
# =========================================================================

if __name__ == "__main__":
    import os

    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        print("Set MEMORY_ID env var to run smoke test against a real Memory resource.")
        exit(0)

    client = MemoryClient(
        memory_id=memory_id,
        region=os.environ.get("AWS_REGION", "us-west-2"),
    )

    print("Testing hydrate_session_context for user-maria-001 at globex-corp...")
    context = hydrate_session_context(
        memory_client=client,
        tenant_id="globex-corp",
        user_id="user-maria-001",
    )
    if context:
        print("Hydrated context:\n")
        print(context)
    else:
        print("No prior memory for this user (cold start).")
