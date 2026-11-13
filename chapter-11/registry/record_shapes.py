"""
record_shapes.py — Build AWS Agent Registry records in the GA (`agent-registry`) shape.

Accompanies Chapter 11 of "AgentCore in Production: The Operator's Playbook",
2nd edition (October 2026).

Why this file exists:
    The first edition targeted the April 2026 preview, where the Registry lived
    in the `bedrock-agentcore` namespace and each of four `descriptorType`
    values (MCP, A2A, AGENT_SKILLS, CUSTOM) wanted a differently nested
    descriptor. That namespace shut down on October 30, 2026. At GA (August 31,
    2026) the Registry lives in its own namespace, `agent-registry`, and the
    record model was restructured (see the AWS migration guide, "Change 2" and
    "Change 3"):

      * `name` is now a deduplication key, unique within the registry.
        The human-readable label moved to `displayName`.
      * `recordType` (required) replaces `descriptorType`:
        AGENT | MCP | SKILL | CUSTOM  (GATEWAY is assigned by auto-detection).
      * `descriptors` is a flat map with exactly ONE primary key per record:

            recordType  valid primary descriptor
            ----------  ------------------------------------------
            AGENT       a2aAgentCard | mcpServer | custom
            MCP         mcpServer | custom
            SKILL       agentSkillsDefinition | custom
            CUSTOM      custom

      * `inlineContent` became `data`; `schemaVersion` / `protocolVersion`
        became `dataSchemaVersion`.
      * Supplementary content nests under the primary descriptor's
        `additionalData`: the MCP tool list (`tools`) and the skill's
        Markdown (`skillMd`).

    The shapes are simpler than in preview, so this module is thinner than it
    was. It keeps two jobs: building the right descriptor for each record type,
    and refusing records that a catalog should never contain (no owner, no
    contact, a description nobody could find with a search).

VERIFICATION STATUS: shapes and parameter names validated offline against the
botocore 1.43 `agent-registry-control` service model (see repo/_verify), and
exercised against the live service in us-east-1 on 2026-11-10. The live service validates `data` against the schema named by
`dataSchemaVersion`, and the first draft of this module failed that check for
three of the four record types:

  * A2A `0.3` requires the full Agent Card: protocolVersion, name,
    description, url, version, capabilities, defaultInputModes,
    defaultOutputModes, and skills, where every skill needs id, name,
    description, and tags. Dropping any one of them is rejected.
  * An MCP tool list is validated as MCP tools (`2025-06-18`): every tool
    needs an `inputSchema`.
  * `skillMd` frontmatter must contain a `description` field.

Requires:
    boto3 recent enough to include the `agent-registry-control` client
    (any 2026-08 or later release).
"""

import json
from dataclasses import dataclass, field
from typing import Any

# =========================================================================
# Record types (GA)
# =========================================================================

TYPE_AGENT = "AGENT"
TYPE_MCP = "MCP"
TYPE_SKILL = "SKILL"
TYPE_CUSTOM = "CUSTOM"

AUTHORABLE_TYPES = (TYPE_AGENT, TYPE_MCP, TYPE_SKILL, TYPE_CUSTOM)
# GATEWAY exists too, but only auto-detection creates it.

# Versions from AWS's GA sample. Not author-verified against the live service.
MCP_SERVER_SCHEMA_VERSION = "2025-12-11"
MCP_TOOLS_SCHEMA_VERSION = "2025-06-18"
A2A_CARD_SCHEMA_VERSION = "0.3"

# Fields the Registry enforces on an A2A 0.3 Agent Card (live-tested 2026-11-10).
A2A_REQUIRED_CARD_FIELDS = (
    "protocolVersion", "name", "description", "url", "version",
    "capabilities", "defaultInputModes", "defaultOutputModes", "skills",
)

GENERIC_WORDS = {"tool", "agent", "service", "api", "helper", "util"}


# =========================================================================
# The normalized record
# =========================================================================

@dataclass
class RegistryRecord:
    """A record in the shape this repository uses internally.

    `owner` and `contact` are not Registry fields. They are folded into the
    description (which search indexes) because a catalog entry whose owner is
    unknown is the problem the Registry exists to solve.
    """
    name: str                        # dedup key: [a-zA-Z0-9][a-zA-Z0-9_\-./]*
    display_name: str
    record_type: str                 # AGENT | MCP | SKILL | CUSTOM
    description: str
    owner: str
    contact: str
    payload: dict[str, Any]          # type-specific content, see builders
    record_version: str = "1.0.0"
    tags: dict[str, str] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.record_type not in AUTHORABLE_TYPES:
            problems.append(
                f"record type '{self.record_type}' is not authorable "
                f"(use one of {AUTHORABLE_TYPES}; GATEWAY comes from auto-detection)"
            )
        if not self.owner:
            problems.append("owner is required — an unowned record is a future orphan")
        if not self.contact:
            problems.append("contact is required — who gets paged when this breaks?")
        if len(self.description) < 40:
            problems.append(
                "description is too short. Registry search is only as good as the "
                "worst description indexed; write what a consumer would type as a query"
            )
        words = {w.strip(".,").lower() for w in self.description.split()}
        if words & GENERIC_WORDS and len(words) < 15:
            problems.append(
                "description leans on generic words without enough context "
                "(tool/agent/service). Be specific about domain and capability"
            )
        problems.extend(self._validate_payload())
        return problems

    def _validate_payload(self) -> list[str]:
        problems: list[str] = []
        p = self.payload
        if self.record_type == TYPE_AGENT and "agentCard" in p:
            card = p["agentCard"]
            for key in A2A_REQUIRED_CARD_FIELDS:
                if key not in card:
                    problems.append(f"A2A Agent Card is missing '{key}'")
            for skill in card.get("skills", []):
                for key in ("id", "name", "description", "tags"):
                    if key not in skill:
                        problems.append(
                            f"A2A skill '{skill.get('id', '?')}' is missing '{key}'"
                        )
        if self.record_type == TYPE_MCP and "server" not in p:
            problems.append("MCP record requires a 'server' object (name, description, version)")
        if self.record_type == TYPE_MCP:
            for tool in p.get("tools") or []:
                if "inputSchema" not in tool:
                    problems.append(
                        f"MCP tool '{tool.get('name', '?')}' is missing 'inputSchema'"
                    )
        if self.record_type == TYPE_SKILL:
            if "skillDefinition" not in p:
                problems.append("SKILL record requires 'skillDefinition' (structured metadata)")
            md = p.get("skillMd")
            if md is not None and not md.lstrip().startswith("---"):
                problems.append("skillMd should start with YAML frontmatter delimited by '---'")
            elif md is not None:
                frontmatter = md.split("---")[1] if md.count("---") >= 2 else ""
                if "description:" not in frontmatter:
                    problems.append("skillMd frontmatter must contain a 'description' field")
        return problems


# =========================================================================
# Descriptor builders — one primary key per record
# =========================================================================

def _agent_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    # AGENT records backed by an A2A server carry the Agent Card. An agent
    # exposed only as an HTTP endpoint can use `custom` instead.
    if "agentCard" in payload:
        return {"a2aAgentCard": {
            "data": json.dumps(payload["agentCard"]),
            "dataSchemaVersion": A2A_CARD_SCHEMA_VERSION,
        }}
    return {"custom": {"data": json.dumps(payload)}}


def _mcp_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "data": json.dumps(payload["server"]),
        "dataSchemaVersion": MCP_SERVER_SCHEMA_VERSION,
    }
    if payload.get("tools") is not None:
        # The tool list is supplementary content, wrapped as {"tools": [...]}.
        descriptor["additionalData"] = {"tools": {
            "data": json.dumps({"tools": payload["tools"]}),
            "dataSchemaVersion": MCP_TOOLS_SCHEMA_VERSION,
        }}
    return {"mcpServer": descriptor}


def _skill_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    descriptor: dict[str, Any] = {"data": json.dumps(payload["skillDefinition"])}
    if "skillMd" in payload:
        # Raw Markdown, not json.dumps'd.
        descriptor["additionalData"] = {"skillMd": {"data": payload["skillMd"]}}
    return {"agentSkillsDefinition": descriptor}


def _custom_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    return {"custom": {"data": json.dumps(payload)}}


_BUILDERS = {
    TYPE_AGENT: _agent_descriptor,
    TYPE_MCP: _mcp_descriptor,
    TYPE_SKILL: _skill_descriptor,
    TYPE_CUSTOM: _custom_descriptor,
}


def build_descriptors(record: RegistryRecord) -> dict[str, Any]:
    problems = record.validate()
    if problems:
        raise ValueError(
            f"Record '{record.name}' is not publishable:\n  " + "\n  ".join(problems)
        )
    return _BUILDERS[record.record_type](record.payload)


def to_create_request(record: RegistryRecord, registry_id: str) -> dict[str, Any]:
    """Build the full CreateRegistryRecord request for `agent-registry-control`."""
    request: dict[str, Any] = {
        "registryId": registry_id,
        "name": record.name,
        "displayName": record.display_name,
        "recordType": record.record_type,
        "recordVersion": record.record_version,
        # Owner and contact folded into the description on purpose: the
        # description is indexed by search, so an orphan is visibly an orphan.
        "description": (
            f"{record.description} "
            f"[owner: {record.owner} | contact: {record.contact}]"
        ),
        "descriptors": build_descriptors(record),
    }
    if record.tags:
        request["tags"] = record.tags
    return request


# =========================================================================
# Example catalog — the Acme SaaS agents from earlier chapters
# =========================================================================

ACME_CATALOG: list[RegistryRecord] = [
    RegistryRecord(
        name="acme-support-agent",
        display_name="Acme support agent",
        record_type=TYPE_AGENT,
        description=(
            "Customer-facing support agent for Acme SaaS. Answers billing, "
            "configuration, and account questions for enterprise tenants, "
            "escalates outages and refund decisions to humans."
        ),
        owner="Squad Support Platform",
        contact="#acme-support-platform",
        payload={"agentCard": {
            "protocolVersion": "0.3.0",
            "name": "acme-support-agent",
            "description": "Enterprise customer support agent, multi-tenant.",
            "url": "https://agents.acme.example/support",
            "version": "2.4.0",
            "capabilities": {},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {"id": "billing-explain", "name": "Explain billing",
                 "description": "Explains invoices, proration, and plan changes.",
                 "tags": ["billing", "invoices"]},
                {"id": "escalate", "name": "Escalate to human",
                 "description": "Routes outages, refunds, and legal topics to a human.",
                 "tags": ["escalation", "refunds"]},
            ],
        }},
        tags={"environment": "production", "tier": "customer-facing"},
    ),
    RegistryRecord(
        name="acme-runbook-generator",
        display_name="Acme runbook generator",
        record_type=TYPE_AGENT,
        description=(
            "Long-running agent that analyzes 30 days of support tickets, "
            "identifies recurring operational patterns, and writes runbooks "
            "as Markdown committed to the platform runbook repository."
        ),
        owner="Squad Platform",
        contact="carla@acme.example",
        payload={"agentCard": {
            "protocolVersion": "0.3.0",
            "name": "acme-runbook-generator",
            "description": "Batch runbook generation from ticket history.",
            "url": "https://agents.acme.example/runbooks",
            "version": "1.3.0",
            "capabilities": {},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {"id": "analyze-tickets", "name": "Analyze ticket corpus",
                 "description": "Clusters tickets into recurring operational patterns.",
                 "tags": ["tickets", "analysis"]},
                {"id": "write-runbook", "name": "Write runbook",
                 "description": "Produces a Markdown runbook and commits it to Git.",
                 "tags": ["runbooks", "git"]},
            ],
        }},
        tags={"environment": "production", "tier": "internal"},
    ),
    RegistryRecord(
        name="acme-billing-mcp",
        display_name="Acme billing MCP server",
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
                 "description": "Retrieve an invoice by ID for the calling tenant.",
                 "inputSchema": {"type": "object",
                                 "properties": {"invoice_id": {"type": "string"}},
                                 "required": ["invoice_id"]}},
                {"name": "get_plan_history",
                 "description": "Return the tenant's plan changes with effective dates.",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "calculate_proration",
                 "description": "Compute prorated charges for a mid-cycle plan change.",
                 "inputSchema": {"type": "object",
                                 "properties": {"new_plan": {"type": "string"},
                                                "change_date": {"type": "string"}},
                                 "required": ["new_plan"]}},
            ],
        },
        tags={"environment": "production", "data-classification": "confidential"},
    ),
    RegistryRecord(
        name="pii-redaction-skill",
        display_name="PII redaction skill",
        record_type=TYPE_SKILL,
        description=(
            "Reusable skill that redacts personally identifiable information "
            "from text before it is written to logs, memory, or tickets. "
            "Wraps the Guardrails PII configuration used across Acme agents."
        ),
        owner="Squad Compliance",
        contact="compliance@acme.example",
        record_version="2.1.0",
        payload={
            "skillDefinition": {
                "name": "pii-redaction",
                "title": "PII Redaction Skill",
                "version": "2.1.0",
                "owner": "compliance@acme.example",
                "tags": ["compliance", "privacy", "pii", "redaction"],
                "package": {"type": "python", "name": "acme-pii-redaction",
                            "version": "2.1.0"},
            },
            "skillMd": (
                "---\n"
                "name: pii-redaction\n"
                "description: Redacts PII from free text before it is logged or stored.\n"
                "version: 2.1.0\n"
                "---\n\n"
                "# PII Redaction Skill\n\n"
                "Redacts personally identifiable information from free text "
                "before it is written to logs, memory, or tickets.\n"
            ),
        },
        tags={"environment": "production", "reusable": "true"},
    ),
    RegistryRecord(
        name="acme-ticket-triage-tool",
        display_name="Acme ticket triage classifier",
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

    print("\nDescriptor shapes produced (GA, agent-registry):\n")
    for rec in ACME_CATALOG:
        shape = build_descriptors(rec)
        primary = next(iter(shape))
        extra = list(shape[primary].get("additionalData", {}).keys())
        print(f"  {rec.record_type:<7} -> {primary}"
              + (f" + additionalData{extra}" if extra else ""))
