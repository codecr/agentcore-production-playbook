"""
memory_helpers.py - Production helpers for AgentCore Memory.

Accompanies Chapter 7 of "AgentCore in Production: The Operator's Playbook"
(2nd edition, October 2026).

Patterns:
  1. hydrate_session_context()  - load preferences, summaries, tenant facts at session start
  2. record_user_event() / record_assistant_event() / record_tool_event()
                                - store turns in short-term memory, PII-filtered
  3. record_tenant_fact_event() - the ONLY write path into the tenant-wide facts namespace
  4. end_session_with_summary() - writes a closing marker event (a convention,
                                  not a documented trigger; see Section 7.4.3)

Identity rule (Section 7.8): tenant_id and actor_id are always resolved by the
application from the authenticated caller (actor_id is the identity provider's
"sub"). Nothing in this module accepts identity from model output.

MemoryClient holds only a data-plane client (bedrock-agentcore). Control-plane
work (create_memory, strategies, TTLs) lives in memory/acme-support-memory.yaml.

Requires: pip install "boto3>=1.43"
STATUS: run against a live memory on 2026-11-11: events, tenant-fact writes, retrieval, and
hydration (summaries are read with namespacePath, the prefix form). The closing-marker helper
and the PII filter on writes were not exercised.
"""

import re
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

import boto3
from botocore.exceptions import ClientError

TENANT_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def paginate(operation: Callable, result_key: str, **kwargs) -> Iterator[dict]:
    """Iterate a nextToken-paginated data-plane operation."""
    token = None
    while True:
        page = operation(**kwargs, **({"nextToken": token} if token else {}))
        yield from page.get(result_key, [])
        token = page.get("nextToken")
        if not token:
            return


# =========================================================================
# Namespaces (must mirror memory/acme-support-memory.yaml)
# =========================================================================

def _check_tenant(tenant_id: str) -> str:
    # Custom namespace values must be lowercase; the template enforces the
    # same regex server-side.
    if not TENANT_VALUE_RE.match(tenant_id):
        raise ValueError(f"tenant_id {tenant_id!r} is not a valid namespace value")
    return tenant_id


def user_namespace(tenant_id: str, actor_id: str, kind: str) -> str:
    """kind is one of: preferences, facts, sessions."""
    return f"/tenant/{_check_tenant(tenant_id)}/actor/{actor_id}/{kind}/"


def tenant_facts_namespace(tenant_id: str) -> str:
    return f"/tenant/{_check_tenant(tenant_id)}/facts/"


def tenant_reflections_namespace(tenant_id: str) -> str:
    return f"/tenant/{_check_tenant(tenant_id)}/incidents/"


# =========================================================================
# MemoryClient - data plane only
# =========================================================================

class MemoryClient:
    def __init__(self, memory_id: str, region: str,
                 guardrail_id: Optional[str] = None,
                 guardrail_version: str = "DRAFT"):
        self.memory_id = memory_id
        self._client = boto3.client("bedrock-agentcore", region_name=region)
        # Optional PII filter on writes (Section 7.8). Use a guardrail whose
        # sensitive-information policy ANONYMIZEs identifiers such as SSNs.
        self._guardrail = None
        if guardrail_id:
            self._guardrail = (boto3.client("bedrock-runtime", region_name=region),
                               guardrail_id, guardrail_version)

    @property
    def raw(self):
        """The underlying data-plane client (used by forget_actor.py)."""
        return self._client

    def retrieve(self, namespace: str, query: str, top_k: int = 5,
                 min_score: float = 0.0, prefix: bool = False) -> list[dict]:
        """Semantic search in one namespace. min_score is the relevance floor
        from the Section 7.5 lab (a 0.39 record still comes back).

        `namespace` matches exactly. Summaries and episodes are stored one
        level down (.../sessions/{sessionId}/), so reading them takes
        prefix=True, which sends namespacePath instead (live-tested 2026-11-11).
        """
        scope = {"namespacePath": namespace} if prefix else {"namespace": namespace}
        try:
            response = self._client.retrieve_memory_records(
                memoryId=self.memory_id,
                searchCriteria={"searchQuery": query, "topK": top_k},
                **scope,
            )
        except ClientError as exc:
            print(f"retrieve failed for {namespace}: {exc}")
            return []
        records = []
        for r in response.get("memoryRecordSummaries", []):
            score = r.get("score", 0.0)
            if score >= min_score:
                records.append({"id": r.get("memoryRecordId", ""),
                                "content": r.get("content", {}).get("text", ""),
                                "score": round(score, 3)})
        return records

    def _filter_pii(self, text: str) -> str:
        if not self._guardrail:
            return text
        runtime, gid, gver = self._guardrail
        result = runtime.apply_guardrail(
            guardrailIdentifier=gid, guardrailVersion=gver, source="INPUT",
            content=[{"text": {"text": text}}],
        )
        if result.get("action") == "GUARDRAIL_INTERVENED" and result.get("outputs"):
            return result["outputs"][0]["text"]
        return text

    def create_event(self, actor_id: str, session_id: str, role: str,
                     content: str, namespace_variables: dict) -> dict:
        """Send one event to short-term memory.

        namespace_variables feeds extractionConfig.namespaceVariables. Chat
        events pass {"tenantid": ...}; tenant-fact writes pass
        {"tenantfacts": ...} only.
        """
        try:
            return self._client.create_event(
                memoryId=self.memory_id,
                actorId=actor_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=[{"conversational": {
                    "role": role,
                    "content": {"text": self._filter_pii(content)},
                }}],
                extractionConfig={"namespaceVariables": namespace_variables},
            )
        except ClientError as exc:
            return {"error": str(exc)}


# =========================================================================
# Pattern 1: hydration (the code missing from the Section 7.1 trace)
# =========================================================================

def hydrate_session_context(memory_client: MemoryClient, tenant_id: str,
                            actor_id: str, min_score: float = 0.0) -> Optional[str]:
    reads = [
        ("User preferences", user_namespace(tenant_id, actor_id, "preferences"),
         "communication and format preferences", 5),
        ("User facts", user_namespace(tenant_id, actor_id, "facts"),
         "account details and setup this user described", 5),
        ("Recent sessions", user_namespace(tenant_id, actor_id, "sessions"),
         "recent support sessions and outcomes", 3),
        ("Tenant facts", tenant_facts_namespace(tenant_id),
         "billing tier, plan, primary contacts", 5),
    ]
    # Summaries sit under .../sessions/{sessionId}/, so that read is by prefix.
    prefix_reads = {"Recent sessions"}
    blocks = []
    for title, namespace, query, top_k in reads:
        records = memory_client.retrieve(namespace, query, top_k, min_score,
                                         prefix=title in prefix_reads)
        if records:
            lines = "\n".join(f"- {r['content']}" for r in records)
            blocks.append(f"## {title}\n{lines}")
    return "\n\n".join(blocks) or None


# =========================================================================
# Pattern 2: per-turn events
# =========================================================================

def _record(memory_client, tenant_id, actor_id, session_id, role, content):
    return memory_client.create_event(
        actor_id=actor_id, session_id=session_id, role=role, content=content,
        namespace_variables={"tenantid": _check_tenant(tenant_id)},
    )


def record_user_event(memory_client, tenant_id, actor_id, session_id, content):
    return _record(memory_client, tenant_id, actor_id, session_id, "USER", content)


def record_assistant_event(memory_client, tenant_id, actor_id, session_id, content):
    return _record(memory_client, tenant_id, actor_id, session_id, "ASSISTANT", content)


def record_tool_event(memory_client, tenant_id, actor_id, session_id, content):
    """Tool outputs are the most valuable input for episodic extraction."""
    return _record(memory_client, tenant_id, actor_id, session_id, "TOOL", content)


# =========================================================================
# Pattern 3: deliberate tenant-fact writes
# =========================================================================

TENANT_FACTS_ACTOR = "acme-tenant-facts-writer"


def record_tenant_fact_event(memory_client: MemoryClient, tenant_id: str,
                             verified_fact: str) -> dict:
    """The only write path into /tenant/{tenantid}/facts/.

    Call it from application code that has verified the fact against a
    system of record (billing, CRM), never from a model tool call. The event
    carries only the tenantfacts variable, so the per-user strategies skip it.
    """
    return memory_client.create_event(
        actor_id=TENANT_FACTS_ACTOR,
        session_id=f"facts{datetime.now(timezone.utc):%Y%m%d}",
        role="ASSISTANT",
        content=verified_fact,
        namespace_variables={"tenantfacts": _check_tenant(tenant_id)},
    )


# =========================================================================
# Pattern 4: closing marker
# =========================================================================

def end_session_with_summary(memory_client, tenant_id, actor_id, session_id):
    """Write a closing marker event.

    AWS documents no explicit session-end trigger for the summary strategy;
    this marker is a transcript convention. Do not build logic that assumes
    it accelerates summarization.
    """
    return _record(memory_client, tenant_id, actor_id, session_id, "ASSISTANT",
                   "[SESSION_END] Conversation closed by the application.")


if __name__ == "__main__":
    import os
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        print("Set MEMORY_ID (and optionally ACTOR_ID) to run the smoke test.")
        raise SystemExit(0)
    client = MemoryClient(memory_id, os.environ.get("AWS_REGION", "us-west-2"))
    print(hydrate_session_context(client, "globex-corp",
                                  os.environ.get("ACTOR_ID", "maria-sub-0001"))
          or "No prior memory for this user (cold start).")
