"""
record_shapes.py — Normalize the four AWS Agent Registry descriptor shapes.

Accompanies Chapter 11 of "AgentCore in Production: The Operator's Playbook".

Why this file exists:
    AWS Agent Registry supports four record types — MCP, A2A, AGENT_SKILLS,
    and CUSTOM — and each one wants its descriptor in a different shape.
    Three distinct patterns across four types. Publishing a handful of
    records by hand means discovering each shape through a sequence of
    ParamValidationError messages; publishing a catalog means writing this
    layer once and never thinking about it again.

    The shapes, as of the April 2026 preview (confirmed against the
    `bedrock-agentcore-control` service model, not guessed from analogy):

        MCP           {"mcp": {"server": {"inlineContent": "..."},
                                "tools":  {"inlineContent": "..."}}}
                      `server` describes the MCP server itself (name,
                      description, version); `tools` is the tool list,
                      wrapped one level deeper as {"tools": [...]}. Both
                      sub-keys carry `inlineContent`, never `schema`.
        A2A           {"a2a": {"agentCard": {"inlineContent": "..."}}}
                      ...and the AgentCard requires `protocolVersion`, which
                      fails with a cryptic error when omitted. The sub-key is
                      `agentCard`, never `schema`.
        AGENT_SKILLS  {"agentSkills": {"skillDefinition": {"inlineContent": "..."},
                                       "skillMd":         {"inlineContent": "..."}}}
                      Plural key (`agentSkills`), two sub-keys — structured
                      metadata in `skillDefinition`, markdown documentation in
                      `skillMd` (which must start with YAML frontmatter
                      delimited by `---`). The descriptorType enum value is
                      `AGENT_SKILLS` (plural, uppercase-with-underscore), not
                      `SKILL` or `AgentSkills`.
        CUSTOM        {"custom":       {"inlineContent": "..."}}  <- flat, no
                      intermediate sub-key. Breaks the pattern of the others.

    This module hides all of that behind one function per type plus a
    validation pass that catches the common omissions locally, before the
    API does.

PREVIEW NOTE: The Registry is in preview and the descriptor shapes are the
part of the API most likely to change before GA. If AWS homogenizes them,
delete this module and celebrate. Until then, centralize the weirdness.

Requires:
    boto3 >= 1.42.87   (earlier SDKs do not have the registry methods at all)
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional


# =========================================================================
# Record type constants
# =========================================================================

TYPE_MCP = "MCP"
TYPE_A2A = "A2A"
TYPE_AGENT_SKILLS = "AGENT_SKILLS"
TYPE_CUSTOM = "CUSTOM"

ALL_TYPES = (TYPE_MCP, TYPE_A2A, TYPE_AGENT_SKILLS, TYPE_CUSTOM)


# =========================================================================
# The normalized record
# =========================================================================

@dataclass
class RegistryRecord:
    """A record in the shape this repository uses internally.

    `owner` and `contact` are not Registry fields — they live inside the
    description and the custom payload. They are required here because a
    catalog entry whose owner is unknown is the exact problem the Registry
    exists to solve, and the audit script in observability/ reports on them.
    """
    name: str
    record_type: str
    description: str
    owner: str                       # squad or team name
    contact: str                     # email, Slack channel, or rota
    payload: dict[str, Any]          # type-specific content
    tags: dict[str, str] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.record_type not in ALL_TYPES:
            problems.append(f"unknown record type '{self.record_type}'")
        if not self.owner:
            problems.append("owner is required — an unowned record is a future orphan")
        if not self.contact:
            problems.append("contact is required — who gets paged when this breaks?")
        if len(self.description) < 40:
            problems.append(
                "description is too short. Registry search is only as good as the "
                "worst descriptor indexed; write what a consumer would type as a query"
            )
        generic = {"tool", "agent", "service", "api", "helper", "util"}
        words = {w.strip(".,").lower() for w in self.description.split()}
        if words & generic and len(words) < 15:
            problems.append(
                "description leans on generic words without enough context "
                "(tool/agent/service). Be specific about domain and capability"
            )
        problems.extend(self._validate_payload())
        return problems

    def _validate_payload(self) -> list[str]:
        """Catch the omissions that the API reports with unhelpful errors."""
        problems: list[str] = []
        if self.record_type == TYPE_A2A:
            if "protocolVersion" not in self.payload:
                problems.append(
                    "A2A AgentCard requires 'protocolVersion'; the API error for "
                    "its absence does not name the field"
                )
            if "skills" not in self.payload:
                problems.append("A2A AgentCard expects a 'skills' list")
        if self.record_type == TYPE_MCP:
            if "server" not in self.payload:
                problems.append(
                    "MCP descriptor requires a 'server' object (name, description, "
                    "version) — 'server' is the required sub-key, 'tools' is optional"
                )
        if self.record_type == TYPE_AGENT_SKILLS:
            if "skillDefinition" not in self.payload:
                problems.append(
                    "AGENT_SKILLS requires 'skillDefinition' (structured metadata: "
                    "name, title, version, owner, package)"
                )
            if "skillMd" not in self.payload:
                problems.append(
                    "AGENT_SKILLS requires 'skillMd' (markdown documentation)"
                )
            elif not self.payload["skillMd"].lstrip().startswith("---"):
                problems.append(
                    "AGENT_SKILLS' skillMd must start with YAML frontmatter "
                    "delimited by '---' — the API rejects plain markdown here"
                )
        return problems


# =========================================================================
# Descriptor builders — one per type, hiding the shape differences
# =========================================================================

def _mcp_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    # `server` is required, `tools` is optional. Both sub-keys carry
    # `inlineContent`; `tools` wraps its list one level deeper as
    # {"tools": [...]} rather than being the list itself.
    descriptor: dict[str, Any] = {
        "server": {"inlineContent": json.dumps(payload["server"])}
    }
    tools = payload.get("tools")
    if tools is not None:
        descriptor["tools"] = {"inlineContent": json.dumps({"tools": tools})}
    return {"mcp": descriptor}


def _a2a_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    # Sub-key is `agentCard`, not `schema`. `payload` IS the AgentCard.
    return {"a2a": {"agentCard": {"inlineContent": json.dumps(payload)}}}


def _agent_skills_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    # Plural key (`agentSkills`, camelCase) with two distinct sub-keys, not
    # `inlineContent` directly and not a single `skills` list:
    #   skillDefinition — structured JSON metadata (inlineContent is JSON)
    #   skillMd         — markdown documentation with YAML frontmatter
    #                     (inlineContent is the raw markdown string, NOT
    #                     json.dumps'd)
    # Sending both is recommended: search indexes both sides.
    descriptor: dict[str, Any] = {}
    if "skillDefinition" in payload:
        descriptor["skillDefinition"] = {
            "inlineContent": json.dumps(payload["skillDefinition"])
        }
    if "skillMd" in payload:
        descriptor["skillMd"] = {"inlineContent": payload["skillMd"]}
    return {"agentSkills": descriptor}


def _custom_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    # Flat. No intermediate sub-key. Inferring {"custom": {"schema": ...}}
    # by analogy with MCP produces:
    #   Unknown parameter in descriptors.custom: "schema",
    #   must be one of: inlineContent
    return {"custom": {"inlineContent": json.dumps(payload)}}


_BUILDERS = {
    TYPE_MCP: _mcp_descriptor,
    TYPE_A2A: _a2a_descriptor,
    TYPE_AGENT_SKILLS: _agent_skills_descriptor,
    TYPE_CUSTOM: _custom_descriptor,
}


def build_descriptors(record: RegistryRecord) -> dict[str, Any]:
    """Turn a normalized record into the descriptor shape its type requires."""
    problems = record.validate()
    if problems:
        raise ValueError(
            f"Record '{record.name}' is not publishable:\n  " + "\n  ".join(problems)
        )
    return _BUILDERS[record.record_type](record.payload)


def to_create_request(record: RegistryRecord, registry_id: str) -> dict[str, Any]:
    """Build the full CreateRegistryRecord request."""
    return {
        "registryId": registry_id,
        "name": record.name,
        # The parameter is `descriptorType`, not `type` — botocore raises
        # ParamValidationError for "type" since it is not a recognized
        # member of the CreateRegistryRecord input shape. record_type
        # already holds the correct enum value for every TYPE_* constant
        # (MCP, A2A, AGENT_SKILLS, CUSTOM) confirmed against the service
        # model's descriptorType enum.
        "descriptorType": record.record_type,
        # Owner and contact are folded into the description on purpose: the
        # description is indexed by search, so "owned by Squad Billing" is
        # discoverable, and an orphan record is visibly an orphan.
        "description": (
            f"{record.description} "
            f"[owner: {record.owner} | contact: {record.contact}]"
        ),
        "descriptors": build_descriptors(record),
    }


# =========================================================================
# Example catalog — the Acme SaaS agents from earlier chapters
# =========================================================================
# Every one of these is an agent or tool the book has already described.
# Publishing them is the exercise of turning a book's worth of components
# into a catalog somebody else can find.

ACME_CATALOG: list[RegistryRecord] = [
    RegistryRecord(
        name="acme-support-agent",
        record_type=TYPE_A2A,
        description=(
            "Customer-facing support agent for Acme SaaS. Answers billing, "
            "configuration, and account questions for enterprise tenants, "
            "escalates outages and refund decisions to humans."
        ),
        owner="Squad Support Platform",
        contact="#acme-support-platform",
        payload={
            "protocolVersion": "0.3.0",
            "name": "acme-support-agent",
            "description": "Enterprise customer support agent, multi-tenant.",
            "skills": [
                {"id": "billing-explain", "name": "Explain billing",
                 "description": "Explains invoices, proration, and plan changes."},
                {"id": "escalate", "name": "Escalate to human",
                 "description": "Routes outages, refunds, and legal topics to a human."},
            ],
        },
        tags={"environment": "production", "tier": "customer-facing"},
    ),
    RegistryRecord(
        name="acme-runbook-generator",
        record_type=TYPE_A2A,
        description=(
            "Long-running agent that analyzes 30 days of support tickets, "
            "identifies recurring operational patterns, and writes runbooks "
            "as Markdown committed to the platform runbook repository."
        ),
        owner="Squad Platform",
        contact="carla@acme.example",
        payload={
            "protocolVersion": "0.3.0",
            "name": "acme-runbook-generator",
            "description": "Batch runbook generation from ticket history.",
            "skills": [
                {"id": "analyze-tickets", "name": "Analyze ticket corpus",
                 "description": "Clusters tickets into recurring operational patterns."},
                {"id": "write-runbook", "name": "Write runbook",
                 "description": "Produces a Markdown runbook and commits it to Git."},
            ],
        },
        tags={"environment": "production", "tier": "internal"},
    ),
    RegistryRecord(
        name="acme-billing-mcp",
        record_type=TYPE_MCP,
        description=(
            "MCP server exposing read-only invoice, plan history, and "
            "proration calculation tools for Acme tenants. Tenant scoping is "
            "enforced by Cedar policies at the Gateway, not by this server."
        ),
        owner="Squad Billing",
        contact="#acme-billing",
        payload={
            "server": {
                "name": "acme/acme-billing-mcp",
                "description": "Read-only invoice, plan history, and proration tools.",
                "version": "1.0.0",
            },
            "tools": [
                {"name": "get_invoice",
                 "description": "Retrieve an invoice by ID for the calling tenant."},
                {"name": "get_plan_history",
                 "description": "Return the tenant's plan changes with effective dates."},
                {"name": "calculate_proration",
                 "description": "Compute prorated charges for a mid-cycle plan change."},
            ]
        },
        tags={"environment": "production", "data-classification": "confidential"},
    ),
    RegistryRecord(
        name="pii-redaction-skill",
        record_type=TYPE_AGENT_SKILLS,
        description=(
            "Reusable skill that redacts personally identifiable information "
            "from text before it is written to logs, memory, or tickets. "
            "Wraps the Guardrails PII configuration used across Acme agents."
        ),
        owner="Squad Compliance",
        contact="compliance@acme.example",
        payload={
            "skillDefinition": {
                "name": "pii-redaction",
                "title": "PII Redaction Skill",
                "version": "2.1.0",
                "owner": "compliance@acme.example",
                "tags": ["compliance", "privacy", "pii", "redaction"],
                "package": {
                    "type": "python",
                    "name": "acme-pii-redaction",
                    "version": "2.1.0",
                },
            },
            "skillMd": (
                "---\n"
                "name: pii-redaction\n"
                "version: 2.1.0\n"
                "---\n\n"
                "# PII Redaction Skill\n\n"
                "Redacts personally identifiable information from free text "
                "before it is written to logs, memory, or tickets. Wraps the "
                "Guardrails PII configuration used across Acme agents.\n"
            ),
        },
        tags={"environment": "production", "reusable": "true"},
    ),
    RegistryRecord(
        name="acme-ticket-triage-tool",
        record_type=TYPE_CUSTOM,
        description=(
            "Lambda-backed triage classifier that assigns severity, category, "
            "and escalation flags to inbound support tickets using a strict "
            "JSON schema. Consumed by the support agent and the ticketing system."
        ),
        owner="Squad Platform",
        contact="carla@acme.example",
        payload={
            "endpoint": "arn:aws:lambda:us-east-1:111122223333:function:acme-triage",
            "invocation": "lambda",
            "outputSchema": "ticket_triage_v1",
            "sla": {"p99_latency_ms": 1800},
        },
        tags={"environment": "production"},
    ),
]


if __name__ == "__main__":
    print(f"Validating {len(ACME_CATALOG)} records...\n")
    for rec in ACME_CATALOG:
        problems = rec.validate()
        status = "ok" if not problems else "PROBLEMS"
        print(f"[{status}] {rec.name} ({rec.record_type}) — owner {rec.owner}")
        for p in problems:
            print(f"         - {p}")

    print("\nDescriptor shapes produced:\n")
    for rec in ACME_CATALOG:
        shape = build_descriptors(rec)
        top_key = next(iter(shape))
        inner = list(shape[top_key].keys())
        print(f"  {rec.record_type:<14} -> {{'{top_key}': {inner}}}")
