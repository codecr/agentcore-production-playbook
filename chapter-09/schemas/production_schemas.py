"""
production_schemas.py — A library of JSON Schemas for agent outputs.

Accompanies Chapter 9 of "AgentCore in Production: The Operator's Playbook".

Every schema here is written for Bedrock Structured Outputs (JSON Schema
Draft 2020-12) and follows the design rules from Chapter 9 Section 9.6:

    1. `additionalProperties: false` on every object. Without it, the model
       may add fields you did not ask for, and the contract is only enforced
       in one direction.
    2. `required` lists every property. Optional fields are modeled as
       nullable types, not as absent keys — downstream code should never
       have to ask "is this key present?".
    3. Enums instead of free strings wherever the value space is closed.
       A `priority` field with an enum cannot produce "urgent-ish".
    4. Descriptions on every property. The schema constrains shape; the
       descriptions are what steer semantics. Bedrock passes them to the
       model.
    5. Shallow nesting. Grammar compilation cost and model error rates both
       rise with depth. Three levels is a practical ceiling.

Import and use:

    from schemas.production_schemas import TRIAGE_SCHEMA, as_output_config

    response = bedrock.converse(
        modelId=MODEL_ID,
        messages=messages,
        outputConfig=as_output_config(TRIAGE_SCHEMA, "ticket_triage"),
    )
    result = json.loads(response["output"]["message"]["content"][0]["text"])
"""

import json
from typing import Any


# =========================================================================
# Helper: wrap a schema into the Converse API's outputConfig shape
# =========================================================================

def as_output_config(schema: dict[str, Any], name: str, description: str = "") -> dict:
    """Build the `outputConfig` parameter for the Converse / ConverseStream API.

    The schema must be serialized as a JSON string — Bedrock expects a string
    here, not a nested object. Passing a dict produces a validation error that
    reads like a schema problem when it is actually a serialization problem.
    """
    return {
        "textFormat": {
            "type": "json_schema",
            "structure": {
                "jsonSchema": {
                    "schema": json.dumps(schema),
                    "name": name,
                    "description": description or f"Structured output: {name}",
                }
            },
        }
    }


# =========================================================================
# Schema 1: Ticket triage
# =========================================================================
# The schema from the Chapter 9 opening incident. The Acme SaaS triage agent
# classifies inbound support tickets; the ticketing system consumes the
# result directly. Before Structured Outputs, a parse failure fell through
# to a keyword heuristic that silently mislabeled a P1 outage as P3.
#
# Note the design choices:
#   - `priority` is an enum, not a string. The downstream router switches on
#     it; an unexpected value would land in the default branch.
#   - `confidence` is a bounded number, so a threshold check is meaningful.
#   - `needs_human_review` is an explicit boolean rather than something the
#     router infers from confidence. Making the escalation decision part of
#     the contract means the model states it rather than the caller guessing.

TRIAGE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "priority": {
            "type": "string",
            "enum": ["P1", "P2", "P3", "P4"],
            "description": (
                "Severity. P1 = production outage or data loss affecting "
                "multiple customers. P2 = degraded service or single-customer "
                "outage. P3 = functional issue with a workaround. "
                "P4 = question, request, or cosmetic issue."
            ),
        },
        "category": {
            "type": "string",
            "enum": [
                "billing",
                "authentication",
                "api-error",
                "performance",
                "data-integrity",
                "feature-request",
                "other",
            ],
            "description": "Primary category used for queue routing.",
        },
        "affected_tenant_id": {
            "type": ["string", "null"],
            "description": (
                "Tenant identifier if the ticket names one explicitly. "
                "Null when the ticket does not identify a tenant. Do not "
                "infer a tenant from context."
            ),
        },
        "summary": {
            "type": "string",
            "description": "One sentence, under 140 characters, for the queue list view.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "Confidence in the priority assignment. Below 0.7 the ticket "
                "is routed to a human triager rather than an automated queue."
            ),
        },
        "needs_human_review": {
            "type": "boolean",
            "description": (
                "True when the ticket is ambiguous, mentions legal or "
                "regulatory exposure, or when confidence is below 0.7."
            ),
        },
    },
    "required": [
        "priority",
        "category",
        "affected_tenant_id",
        "summary",
        "confidence",
        "needs_human_review",
    ],
    "additionalProperties": False,
}


# =========================================================================
# Schema 2: Entity extraction with provenance
# =========================================================================
# Extraction is the most common Structured Outputs use case. The addition
# worth copying here is `source_quote`: every extracted value carries the
# span of source text it came from. That turns a downstream spot-check from
# "does this look right?" into "does this quote actually say that?" — and it
# makes the extraction auditable without re-running the model.

EXTRACTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "description": "Every entity found in the source document.",
            "items": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "The extracted value, normalized.",
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": [
                            "invoice_id",
                            "account_number",
                            "date",
                            "amount",
                            "product_name",
                            "person_name",
                            "error_code",
                        ],
                        "description": "Type of the extracted entity.",
                    },
                    "source_quote": {
                        "type": "string",
                        "description": (
                            "Verbatim span from the source document that "
                            "contains this value. Must appear in the source "
                            "exactly. Used for audit and spot-checking."
                        ),
                    },
                },
                "required": ["value", "entity_type", "source_quote"],
                "additionalProperties": False,
            },
        },
        "document_type": {
            "type": "string",
            "enum": ["invoice", "contract", "support_email", "log_excerpt", "unknown"],
            "description": "Best classification of the source document.",
        },
        "extraction_complete": {
            "type": "boolean",
            "description": (
                "False when the document appears truncated or when the model "
                "could not process part of it. Downstream should not treat a "
                "false value as a complete extraction."
            ),
        },
    },
    "required": ["entities", "document_type", "extraction_complete"],
    "additionalProperties": False,
}


# =========================================================================
# Schema 3: Decision with mandatory abstention path
# =========================================================================
# A decision schema whose enum includes an explicit "insufficient_information"
# outcome. This is the schema-level version of the lesson from Chapter 5:
# a system that can only answer yes or no will answer yes or no even when it
# should not answer at all. Giving the model a legitimate way to abstain,
# inside the contract, is what keeps the abstention visible to the caller
# instead of buried in prose.

DECISION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["approve", "deny", "escalate", "insufficient_information"],
            "description": (
                "The outcome. Use 'insufficient_information' when the input "
                "does not contain what the policy requires to decide. Do not "
                "guess in order to produce approve or deny."
            ),
        },
        "policy_basis": {
            "type": "array",
            "description": (
                "Identifiers of the policy clauses this decision rests on. "
                "Empty only when decision is 'insufficient_information'."
            ),
            "items": {"type": "string"},
        },
        "missing_inputs": {
            "type": "array",
            "description": (
                "Fields that would be needed to decide. Empty unless decision "
                "is 'insufficient_information'."
            ),
            "items": {"type": "string"},
        },
        "rationale": {
            "type": "string",
            "description": "Two sentences at most, written for an auditor.",
        },
    },
    "required": ["decision", "policy_basis", "missing_inputs", "rationale"],
    "additionalProperties": False,
}


# =========================================================================
# Schema 4: A strict tool input definition
# =========================================================================
# Used with the `strict: true` flag on a tool definition rather than with
# outputConfig. This is the mechanism that matters most for agents: it
# constrains the ARGUMENTS the model produces for a tool call, so malformed
# arguments never reach the Gateway.
#
# Pair it with Cedar policies (Chapter 3). Strict tool schemas guarantee the
# call is well-formed; Cedar decides whether a well-formed call is allowed.
# They are different questions and both need answering.

REFUND_TOOL_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "issue_refund",
        "description": (
            "Issue a refund against an invoice. The amount must not exceed "
            "the invoice total. Requires an approved reason code."
        ),
        "strict": True,
        "inputSchema": {
            "json": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "invoice_id": {
                        "type": "string",
                        "pattern": "^INV-[0-9]{8}$",
                        "description": "Invoice identifier in INV-NNNNNNNN form.",
                    },
                    "amount_cents": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Refund amount in cents. Integer cents, never a "
                            "float — float money is a rounding incident "
                            "waiting to happen."
                        ),
                    },
                    "currency": {
                        "type": "string",
                        "enum": ["USD", "EUR", "CRC"],
                        "description": "ISO currency code. Must match the invoice.",
                    },
                    "reason_code": {
                        "type": "string",
                        "enum": [
                            "billing_error",
                            "service_credit",
                            "duplicate_charge",
                            "goodwill",
                        ],
                        "description": "Approved reason code for the refund ledger.",
                    },
                },
                "required": ["invoice_id", "amount_cents", "currency", "reason_code"],
                "additionalProperties": False,
            }
        },
    }
}


# =========================================================================
# Registry of everything in this module
# =========================================================================

ALL_SCHEMAS: dict[str, dict[str, Any]] = {
    "ticket_triage": TRIAGE_SCHEMA,
    "entity_extraction": EXTRACTION_SCHEMA,
    "policy_decision": DECISION_SCHEMA,
}

ALL_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "issue_refund": REFUND_TOOL_SPEC,
}


if __name__ == "__main__":
    for schema_name, schema in ALL_SCHEMAS.items():
        props = len(schema.get("properties", {}))
        print(f"{schema_name:<20} {props} properties, "
              f"additionalProperties={schema.get('additionalProperties')}")
    for tool_name, spec in ALL_TOOL_SPECS.items():
        strict = spec["toolSpec"].get("strict")
        print(f"{tool_name:<20} tool spec, strict={strict}")
