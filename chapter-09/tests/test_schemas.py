"""
test_schemas.py — Tests for the production schema library.

Accompanies Chapter 9 of "AgentCore in Production: The Operator's Playbook".

These tests run without AWS credentials and without network access. That is
the point: a schema is code, and the failure modes that matter most —
illegal Draft 2020-12 syntax, a missing additionalProperties, a breaking
change shipped without a version bump — are all detectable locally, before
a single Bedrock request is made.

Run:
    pip install pytest jsonschema
    pytest tests/test_schemas.py -v
"""

import copy
import json
import os
import sys

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from schemas.production_schemas import (  # noqa: E402
    ALL_SCHEMAS,
    ALL_TOOL_SPECS,
    TRIAGE_SCHEMA,
    DECISION_SCHEMA,
    EXTRACTION_SCHEMA,
    as_output_config,
)
from utils.schema_registry import (  # noqa: E402
    SchemaRegistry,
    build_default_registry,
    compare_versions,
    validate_schema,
)


# =========================================================================
# Every schema must be legal Draft 2020-12
# =========================================================================

@pytest.mark.parametrize("name,schema", sorted(ALL_SCHEMAS.items()))
def test_schema_is_legal_draft_2020_12(name, schema):
    """Bedrock returns HTTP 400 for an illegal schema. Catch it here instead."""
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("name,spec", sorted(ALL_TOOL_SPECS.items()))
def test_tool_input_schema_is_legal(name, spec):
    Draft202012Validator.check_schema(spec["toolSpec"]["inputSchema"]["json"])


# =========================================================================
# Every schema must follow the production conventions
# =========================================================================

@pytest.mark.parametrize("name,schema", sorted(ALL_SCHEMAS.items()))
def test_schema_follows_conventions(name, schema):
    problems = validate_schema(schema)
    assert not problems, f"{name} violates conventions:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("name,spec", sorted(ALL_TOOL_SPECS.items()))
def test_tool_specs_are_strict(name, spec):
    """A tool definition without strict=true is not schema-enforced."""
    assert spec["toolSpec"].get("strict") is True, (
        f"{name}: tool specs in production must set strict=true, otherwise "
        f"the model can produce arguments that do not match inputSchema"
    )


# =========================================================================
# Sample payloads must validate — and counter-examples must not
# =========================================================================

VALID_TRIAGE = {
    "priority": "P1",
    "category": "api-error",
    "affected_tenant_id": "globex-corp",
    "summary": "All API requests returning 503 since 02:10 UTC.",
    "confidence": 0.93,
    "needs_human_review": False,
}


def test_valid_triage_payload_validates():
    Draft202012Validator(TRIAGE_SCHEMA).validate(VALID_TRIAGE)


def test_triage_rejects_unknown_priority():
    """The enum is what stops 'urgent-ish' from reaching the router."""
    payload = {**VALID_TRIAGE, "priority": "URGENT"}
    with pytest.raises(Exception):
        Draft202012Validator(TRIAGE_SCHEMA).validate(payload)


def test_triage_rejects_extra_field():
    """additionalProperties=false is what stops silent contract drift."""
    payload = {**VALID_TRIAGE, "suggested_owner": "carla"}
    with pytest.raises(Exception):
        Draft202012Validator(TRIAGE_SCHEMA).validate(payload)


def test_triage_rejects_out_of_range_confidence():
    payload = {**VALID_TRIAGE, "confidence": 1.4}
    with pytest.raises(Exception):
        Draft202012Validator(TRIAGE_SCHEMA).validate(payload)


def test_triage_accepts_null_tenant():
    """Absent-vs-null matters. The schema models 'unknown tenant' as null,
    so downstream code never has to check for a missing key."""
    payload = {**VALID_TRIAGE, "affected_tenant_id": None}
    Draft202012Validator(TRIAGE_SCHEMA).validate(payload)


def test_decision_schema_has_abstention_path():
    """A decision schema without an abstention option forces a guess."""
    options = set(DECISION_SCHEMA["properties"]["decision"]["enum"])
    assert "insufficient_information" in options


def test_extraction_requires_source_quote():
    """Provenance is what makes an extraction auditable after the fact."""
    item_schema = EXTRACTION_SCHEMA["properties"]["entities"]["items"]
    assert "source_quote" in item_schema["required"]


# =========================================================================
# outputConfig shape
# =========================================================================

def test_output_config_serializes_schema_as_string():
    """Bedrock expects the schema as a JSON string, not a nested object.
    Passing a dict fails with an error that reads like a schema problem."""
    config = as_output_config(TRIAGE_SCHEMA, "ticket_triage")
    schema_field = config["textFormat"]["structure"]["jsonSchema"]["schema"]
    assert isinstance(schema_field, str)
    assert json.loads(schema_field) == TRIAGE_SCHEMA


# =========================================================================
# Registry behavior
# =========================================================================

def test_registry_rejects_invalid_schema():
    registry = SchemaRegistry()
    bad = {"type": "object", "properties": {"x": {"type": "string"}}}  # no additionalProperties
    with pytest.raises(ValueError):
        registry.register("bad", 1, bad)


def test_registry_qualified_name_includes_version():
    """Two different schemas must never share a Bedrock schema name — the
    grammar cache is keyed on it."""
    registry = build_default_registry()
    assert registry.latest("ticket_triage").qualified_name == "ticket_triage_v1"


def test_registry_rejects_duplicate_version():
    registry = build_default_registry()
    with pytest.raises(ValueError):
        registry.register("ticket_triage", 1, TRIAGE_SCHEMA)


# =========================================================================
# Version compatibility — run this in CI on every schema change
# =========================================================================

def test_removing_a_property_is_breaking():
    new = copy.deepcopy(TRIAGE_SCHEMA)
    del new["properties"]["confidence"]
    new["required"].remove("confidence")
    diff = compare_versions(TRIAGE_SCHEMA, new)
    assert any("removed property 'confidence'" in c for c in diff["breaking"])


def test_narrowing_an_enum_is_breaking():
    new = copy.deepcopy(TRIAGE_SCHEMA)
    new["properties"]["priority"]["enum"] = ["P1", "P2", "P3"]
    diff = compare_versions(TRIAGE_SCHEMA, new)
    assert any("removed enum values" in c for c in diff["breaking"])


def test_adding_an_enum_value_is_compatible_for_the_producer():
    """Adding an enum value does not break the schema contract, but it does
    change what consumers can receive. The registry labels it compatible;
    the consumer review is still the team's job."""
    new = copy.deepcopy(TRIAGE_SCHEMA)
    new["properties"]["priority"]["enum"] = ["P0", "P1", "P2", "P3", "P4"]
    diff = compare_versions(TRIAGE_SCHEMA, new)
    assert not diff["breaking"]
    assert any("added enum values" in c for c in diff["compatible"])


def test_adding_a_required_property_is_breaking():
    new = copy.deepcopy(TRIAGE_SCHEMA)
    new["properties"]["assigned_team"] = {"type": "string", "description": "Owning team."}
    new["required"].append("assigned_team")
    diff = compare_versions(TRIAGE_SCHEMA, new)
    assert any("added required property" in c for c in diff["breaking"])


# =========================================================================
# Version compatibility — recursion into nested schema structures
#
# A previous audit found that compare_versions only inspected top-level
# properties. A breaking change nested inside an array's `items`, or inside
# a nested object's own `properties`, was invisible: compare_versions
# reported {"breaking": [], "compatible": []} even though a real consumer
# relying on the removed field would break. These tests pin the fix.
# =========================================================================

def test_removing_a_required_field_from_array_items_is_breaking():
    """entities[].source_quote is what makes an extraction auditable. Losing
    it from the item schema is exactly as breaking as losing a top-level
    property, and must be reported with a path that says where."""
    new = copy.deepcopy(EXTRACTION_SCHEMA)
    del new["properties"]["entities"]["items"]["properties"]["source_quote"]
    new["properties"]["entities"]["items"]["required"].remove("source_quote")
    diff = compare_versions(EXTRACTION_SCHEMA, new)
    assert any(
        "removed property 'entities[].source_quote'" in c for c in diff["breaking"]
    )


def test_demoting_a_nested_required_field_to_optional_is_breaking():
    """The quiet version of the test above, and the one that used to slip
    through.

    Here `source_quote` is still declared in the item schema — only its
    entry in `required` is dropped. The "removed property" branch therefore
    never fires, so this case is caught only if `required` itself is
    compared at every nesting level. Before that comparison existed,
    compare_versions returned {"breaking": [], "compatible": []} for this
    diff: a silent false negative on the field that makes an extraction
    auditable.
    """
    new = copy.deepcopy(EXTRACTION_SCHEMA)
    new["properties"]["entities"]["items"]["required"].remove("source_quote")
    # The property definition is deliberately left in place.
    assert "source_quote" in new["properties"]["entities"]["items"]["properties"]

    diff = compare_versions(EXTRACTION_SCHEMA, new)
    assert diff["breaking"], "required -> optional must not be silent"
    assert any(
        "entities[].source_quote' changed from required to optional" in c
        for c in diff["breaking"]
    ), diff["breaking"]


def test_demoting_a_top_level_required_field_to_optional_is_breaking():
    """Same hole, top level: consumers written against the old schema may
    have assumed the field was always present."""
    new = copy.deepcopy(TRIAGE_SCHEMA)
    new["required"] = [r for r in new["required"] if r != "needs_human_review"]
    diff = compare_versions(TRIAGE_SCHEMA, new)
    assert any(
        "'needs_human_review' changed from required to optional" in c
        for c in diff["breaking"]
    ), diff["breaking"]


def test_narrowing_an_enum_inside_array_items_is_breaking():
    """entity_type lives inside entities[], not at the top level. Narrowing
    its enum has to be caught by the same recursive walk that catches the
    removed-property case above."""
    new = copy.deepcopy(EXTRACTION_SCHEMA)
    new["properties"]["entities"]["items"]["properties"]["entity_type"]["enum"] = [
        "invoice_id",
        "account_number",
    ]
    diff = compare_versions(EXTRACTION_SCHEMA, new)
    assert any(
        "entities[].entity_type' removed enum values" in c for c in diff["breaking"]
    )


def test_adding_a_required_field_inside_array_items_is_breaking():
    """A new required field on the item schema is just as much a contract
    change for consumers as a new required field at the top level."""
    new = copy.deepcopy(EXTRACTION_SCHEMA)
    new["properties"]["entities"]["items"]["properties"]["confidence"] = {
        "type": "number",
        "description": "Extraction confidence for this entity.",
    }
    new["properties"]["entities"]["items"]["required"].append("confidence")
    diff = compare_versions(EXTRACTION_SCHEMA, new)
    assert any(
        "added required property 'entities[].confidence'" in c
        for c in diff["breaking"]
    )


def test_removing_a_property_inside_a_nested_object_is_breaking():
    """Recursion has to work for a nested `type: object` property, not just
    for array items. Synthetic schema, since none of the production schemas
    happen to nest a plain object inside another object."""
    old = {
        "properties": {
            "contact": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                },
                "required": ["email", "phone"],
            },
        },
        "required": ["contact"],
    }
    new = copy.deepcopy(old)
    del new["properties"]["contact"]["properties"]["phone"]
    new["properties"]["contact"]["required"].remove("phone")
    diff = compare_versions(old, new)
    assert any("removed property 'contact.phone'" in c for c in diff["breaking"])


# =========================================================================
# Version compatibility — order-insensitive type comparison
# =========================================================================

def test_reordered_nullable_type_list_is_not_breaking():
    """["string", "null"] and ["null", "string"] are the same type
    constraint. A naive != comparison would flag this as a breaking type
    change; it must not be."""
    old = {"properties": {"x": {"type": ["string", "null"]}}, "required": ["x"]}
    new = copy.deepcopy(old)
    new["properties"]["x"]["type"] = ["null", "string"]
    diff = compare_versions(old, new)
    assert not diff["breaking"]
    assert not any("changed type" in c for c in diff["compatible"])


def test_genuinely_different_type_is_still_breaking():
    """The order-insensitive comparison must not swallow a real type
    change — only the reordering case is exempt."""
    old = {"properties": {"x": {"type": "string"}}, "required": ["x"]}
    new = {"properties": {"x": {"type": "integer"}}, "required": ["x"]}
    diff = compare_versions(old, new)
    assert any("changed type" in c for c in diff["breaking"])


# =========================================================================
# Version compatibility — existing optional property becoming required
# =========================================================================

def test_existing_optional_property_becoming_required_is_breaking():
    """A property that already existed as optional and becomes required is
    a real breaking change: a consumer that omitted it (or a producer that
    emitted it as null) is now out of contract, even though the property
    name was never added or removed."""
    old = {
        "properties": {"assigned_team": {"type": "string"}},
        "required": [],
    }
    new = {
        "properties": {"assigned_team": {"type": "string"}},
        "required": ["assigned_team"],
    }
    diff = compare_versions(old, new)
    assert any(
        "'assigned_team' changed from optional to required" in c
        for c in diff["breaking"]
    )


def test_already_required_property_staying_required_is_not_flagged():
    """No false positive: a property required in both old and new must not
    show up as an optional-to-required transition."""
    old = {"properties": {"priority": {"type": "string"}}, "required": ["priority"]}
    new = copy.deepcopy(old)
    diff = compare_versions(old, new)
    assert not diff["breaking"]


# =========================================================================
# Version compatibility — widening (enum constraint removed) is compatible
# =========================================================================

def test_removing_an_enum_constraint_entirely_is_labeled_compatible():
    """Dropping the enum so any string is now allowed is a widening, not a
    narrowing — every value the old enum permitted is still permitted. This
    used to be silent (neither breaking nor compatible); it should now be
    recorded so consumers get an explicit compatible-change entry instead
    of no record at all."""
    old = {
        "properties": {"category": {"type": "string", "enum": ["a", "b"]}},
        "required": ["category"],
    }
    new = {
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
    }
    diff = compare_versions(old, new)
    assert not diff["breaking"]
    assert any(
        "removed enum constraint on 'category' (widening, compatible)" in c
        for c in diff["compatible"]
    )
