"""
support_agent_with_memory.py — Acme SaaS customer support agent with AgentCore Memory.

Accompanies Chapter 7 of "AgentCore in Production: The Operator's Playbook".

What this agent does:
    A multi-tenant customer support agent that remembers user preferences
    and tenant facts across sessions. The agent that, had it existed in
    Chapter 7 Section 7.1, would have prevented the "customer who felt
    forgotten" complaint — because it loads user preferences and recent
    session summaries on every new conversation.

Memory integration pattern:
    1. On session start: load user preferences and tenant facts into context
    2. During conversation: send each turn as an event to short-term memory
    3. On session end: trigger summary generation for the session
    4. Periodically: retrieve episodic patterns relevant to the current case

Requires (in the agent container):
    pip install strands-agents strands-agents-tools bedrock-agentcore boto3
"""

import os
import sys
from typing import Any

# memory_helpers.py lives in chapter-07/utils/, one directory up from this
# file, not next to it — a bare `from memory_helpers import ...` fails with
# ModuleNotFoundError at container runtime. Fixed the same way
# chapter-09/agent/triage_agent.py resolves its own agent/ vs. schemas/
# split: sys.path.insert + a package-qualified import, rather than adding
# a Dockerfile that copies agent/ and utils/ into one flat directory.
# chapter-07 had no Dockerfile of its own before this fix, so this is the
# smaller, more consistent change (see README.md for the resulting
# container-packaging requirement: agent/ and utils/ must ship as
# siblings).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

# Local helpers for memory retrieval/storage patterns.
from utils.memory_helpers import (
    MemoryClient,
    actor_id_for,
    tenant_facts_namespace,
    hydrate_session_context,
    record_user_event,
    record_assistant_event,
    record_tenant_fact_event,
    end_session_with_summary,
)


# =========================================================================
# Configuration
# =========================================================================

MEMORY_ID = os.environ["MEMORY_ID"]                # From CFN output
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-7-v1:0"


# =========================================================================
# Memory-aware tools available to the agent
# =========================================================================
# These tools let the agent query Memory directly when it needs to during
# reasoning, rather than relying only on session-start hydration.

memory_client = MemoryClient(memory_id=MEMORY_ID, region=AWS_REGION)


@tool
def get_user_preferences(tenant_id: str, user_id: str) -> dict:
    """Retrieve this user's stated communication preferences.

    Use this if the agent is uncertain how the user prefers to receive
    information (concise vs. detailed, with or without disclaimers, in
    English or Spanish, etc.). Returns up to 5 most relevant preference
    records from long-term memory.
    """
    namespace = f"users/{actor_id_for(tenant_id, user_id)}/preferences"
    return memory_client.retrieve(
        namespace=namespace,
        query="user communication preferences and format choices",
        max_results=5,
    )


@tool
def get_tenant_facts(tenant_id: str, topic: str) -> dict:
    """Retrieve known facts about the tenant on a specific topic.

    Use this when the agent needs context that the conversation has not
    yet provided. Example queries: "billing plan", "primary contact",
    "support tier", "renewal date".

    Reads the AcmeTenantFacts strategy's sentinel-actor namespace — see
    memory_helpers.tenant_facts_namespace and record_tenant_fact.
    """
    namespace = tenant_facts_namespace(tenant_id)
    return memory_client.retrieve(
        namespace=namespace,
        query=topic,
        max_results=10,
    )


@tool
def record_tenant_fact(tenant_id: str, fact: str) -> dict:
    """Record a durable fact that applies to the whole tenant, not just
    the current user (e.g., "Globex Corp is on the Business tier",
    "Globex Corp switched to annual billing in March").

    Use this when the agent learns something every user at this tenant
    should benefit from knowing — not user-specific preferences or
    facts, which are captured automatically from the conversation via
    the AcmeUserPreferences / AcmeUserFacts strategies.
    """
    return record_tenant_fact_event(
        memory_client=memory_client,
        tenant_id=tenant_id,
        content=fact,
    )


@tool
def get_similar_past_incidents(tenant_id: str, user_id: str, situation_summary: str) -> dict:
    """Find this user's cross-episode reflections from past incidents.

    Use this when the user describes an issue that may have been seen
    before. Returns episodic reflection records (situation + resolution
    pattern) ranked by similarity to the supplied situation summary.

    NOTE: Episodic memory has minutes-not-seconds generation latency.
    Recent incidents may not yet have generated episodes; do not rely
    on incident N-1 being available for incident N.

    NOTE: Reflections are scoped per user (actorId), not per tenant —
    the real AgentCore Memory API has no tenant-only namespace
    placeholder independent of actorId. A brand-new user at an existing
    tenant starts with no episodic history of their own, even if other
    users at the same tenant have handled similar incidents.
    """
    namespace = f"users/{actor_id_for(tenant_id, user_id)}/incidents"
    return memory_client.retrieve(
        namespace=namespace,
        query=situation_summary,
        max_results=3,
    )


# =========================================================================
# Agent setup
# =========================================================================

SYSTEM_PROMPT = """You are the Acme SaaS Customer Support Agent.

You help enterprise customers resolve billing questions, technical
issues, and account configuration problems. You handle two hundred
enterprise customers; each customer has multiple users with different
roles and preferences.

MEMORY-AWARE BEHAVIOR:
- At the start of each session, you receive context with the user's
  preferences and recent session summaries. Respect those preferences.
- Do not re-ask for information already in memory. If memory says
  "user prefers Business tier responses in bullet format", use that
  format. If memory says "Globex Corp is on the Business tier",
  don't ask which tier — confirm if uncertain.
- If you need facts not in the hydrated context, call get_tenant_facts
  or get_user_preferences with the relevant query.
- If you learn something durable that applies to the whole tenant, not
  just this user (e.g., a billing plan change), call record_tenant_fact
  so other users at the same tenant benefit from it too.
- If the current situation resembles a past incident, call
  get_similar_past_incidents to see what worked before.

CRITICAL: Do not invent memory. If you don't have a fact in your
context and the tool didn't return it, ask the user — don't guess.
"""


def create_agent() -> Agent:
    """Initialize the Strands agent with memory-aware tools."""
    model = BedrockModel(
        model_id=DEFAULT_MODEL,
        region_name=AWS_REGION,
    )

    return Agent(
        model=model,
        tools=[
            get_user_preferences,
            get_tenant_facts,
            record_tenant_fact,
            get_similar_past_incidents,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


# =========================================================================
# Bedrock AgentCore entry point
# =========================================================================

app = BedrockAgentCoreApp()
_agent = create_agent()


@app.entrypoint
def invoke(payload: dict) -> dict[str, Any]:
    """Entrypoint for each session invocation.

    Expected payload:
        {
            "prompt": "What was my bill last month?",
            "tenant_id": "globex-corp",
            "user_id": "user-maria-001",
            "session_id": "session-abc-123",
            "is_first_turn": true   # Optional; defaults to false
        }
    """
    prompt = payload.get("prompt", "")
    tenant_id = payload.get("tenant_id")
    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    is_first_turn = payload.get("is_first_turn", False)

    if not all([prompt, tenant_id, user_id, session_id]):
        return {
            "response": "Missing required fields: prompt, tenant_id, user_id, session_id.",
            "error": True,
        }

    # On the first turn, hydrate context with preferences and summaries
    # from memory. This is what the customer in Section 7.1 wanted —
    # the agent should already know how they prefer to be addressed.
    context_prefix = ""
    if is_first_turn:
        hydrated = hydrate_session_context(
            memory_client=memory_client,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if hydrated:
            context_prefix = (
                "# Context from prior sessions with this user:\n"
                f"{hydrated}\n\n"
                "# Current user message:\n"
            )

    # Record the user's turn as an event (goes to short-term memory).
    record_user_event(
        memory_client=memory_client,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        content=prompt,
    )

    # Invoke the agent with optional hydrated context.
    full_prompt = context_prefix + prompt if context_prefix else prompt
    result = _agent(full_prompt)
    response_text = str(result)

    # Record the assistant's response.
    record_assistant_event(
        memory_client=memory_client,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        content=response_text,
    )

    return {
        "response": response_text,
        "session_id": session_id,
    }


# =========================================================================
# Optional explicit session end (called by the application layer)
# =========================================================================

def end_session(tenant_id: str, user_id: str, session_id: str) -> dict:
    """Mark the session as ended and trigger summary generation.

    Called by the application when the user explicitly ends the chat
    (closes the window, clicks "End conversation", times out). Triggers
    the asynchronous summary extraction.
    """
    return end_session_with_summary(
        memory_client=memory_client,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )


if __name__ == "__main__":
    app.run()
