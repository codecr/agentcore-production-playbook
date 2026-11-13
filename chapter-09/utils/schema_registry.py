"""
schema_registry.py — Versioned registry for production output schemas.

Accompanies Chapter 9 of "AgentCore in Production: The Operator's Playbook".

Why a registry:
    A schema is a contract between the model and every downstream consumer.
    Contracts change. When they do, the change has to be visible, versioned,
    and reversible — the same discipline Chapter 3 applied to Cedar policies
    and Chapter 4 applied to Guardrail versions.

    Without a registry, schemas live inline in whatever module happens to
    call Bedrock, they drift between services, and a field rename ships to
    production with nothing that could have caught it.

What this provides:
    - Named, versioned schema storage
    - Local validation before the schema ever reaches Bedrock (catch the
      HTTP 400 at test time instead of at request time)
    - Compatibility checking between versions, so a breaking change is
      labeled as one
    - Checks against the Bedrock Structured Outputs subset of Draft 2020-12
      (unsupported keywords, the 12-optional / 16-union limits)
    - `check_ranges`: the post-parse business checks (ranges, lengths) that
      Bedrock does not let a schema express
    - Warm-up support for Bedrock's 24-hour grammar cache, with the 120 s
      read timeout AWS recommends for the first (compiling) request

Requires:
    pip install boto3 jsonschema
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False


logger = logging.getLogger(__name__)


# =========================================================================
# Registry entries
# =========================================================================

@dataclass
class SchemaVersion:
    """One version of one schema."""
    name: str
    version: int
    schema: dict[str, Any]
    description: str = ""
    deprecated: bool = False

    @property
    def qualified_name(self) -> str:
        """The name passed to Bedrock, versioned.

        Not for the grammar cache: Bedrock caches compiled grammars per
        account for identical schemas, so a changed schema compiles fresh
        whatever its name. The version is for everything else that reads
        the name (logs, dashboards, consumers), so two different contracts
        never appear under one name."""
        return f"{self.name}_v{self.version}"


@dataclass
class SchemaRegistry:
    """In-memory registry of schema versions.

    In production this is typically backed by a file in the repository
    (loaded at import time) rather than by a database. The schemas are code;
    they belong in version control next to the code that consumes them.
    """
    _entries: dict[str, list[SchemaVersion]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        version: int,
        schema: dict[str, Any],
        description: str = "",
    ) -> SchemaVersion:
        """Register a schema version after validating it locally."""
        errors = validate_schema(schema)
        if errors:
            raise ValueError(
                f"Schema {name} v{version} is invalid:\n  " + "\n  ".join(errors)
            )

        entry = SchemaVersion(
            name=name, version=version, schema=schema, description=description
        )
        versions = self._entries.setdefault(name, [])
        if any(v.version == version for v in versions):
            raise ValueError(f"Schema {name} v{version} is already registered.")
        versions.append(entry)
        versions.sort(key=lambda v: v.version)
        return entry

    def latest(self, name: str) -> SchemaVersion:
        """Return the highest non-deprecated version of a schema."""
        versions = [v for v in self._entries.get(name, []) if not v.deprecated]
        if not versions:
            raise KeyError(f"No active version registered for schema '{name}'.")
        return versions[-1]

    def get(self, name: str, version: int) -> SchemaVersion:
        for v in self._entries.get(name, []):
            if v.version == version:
                return v
        raise KeyError(f"Schema '{name}' v{version} is not registered.")

    def deprecate(self, name: str, version: int) -> None:
        self.get(name, version).deprecated = True

    def names(self) -> list[str]:
        return sorted(self._entries)

    # ------------------------------------------------------------------
    # Bedrock integration
    # ------------------------------------------------------------------

    def output_config(self, name: str, version: Optional[int] = None) -> dict:
        """Build the Converse API `outputConfig` for a registered schema.

        Delegates to `as_output_config` so there is one place that knows the
        Converse API's outputConfig shape, rather than two copies that can
        drift apart.
        """
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from schemas.production_schemas import as_output_config  # noqa: E402

        entry = self.get(name, version) if version is not None else self.latest(name)
        return as_output_config(entry.schema, entry.qualified_name, entry.description)

    def warm_cache(self, bedrock_client, model_id: str, names: Optional[list[str]] = None) -> dict:
        """Pre-compile grammars for the given schemas.

        Bedrock compiles each new schema into a grammar, which AWS documents
        can take up to about 90 seconds, and caches it for 24 hours from first
        access. On a deploy that introduces or changes schemas, that cost
        lands on whichever user happens to arrive first.

        Calling this at startup, with a trivial prompt, moves the cost to
        deploy time. Pass a client built with `structured_outputs_client()`
        (read timeout >= 120 s) or the first call may time out mid-compile.
        Failures here are not fatal; log and continue.
        """
        results: dict[str, str] = {}
        for schema_name in (names or self.names()):
            try:
                bedrock_client.converse(
                    modelId=model_id,
                    messages=[{"role": "user", "content": [{"text": "warm"}]}],
                    inferenceConfig={"maxTokens": 64},
                    outputConfig=self.output_config(schema_name),
                )
                results[schema_name] = "warmed"
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Cache warm failed for %s: %s", schema_name, exc)
                results[schema_name] = f"failed: {exc}"
        return results


def structured_outputs_client(region: Optional[str] = None):
    """A bedrock-runtime client with the read timeout AWS recommends (>= 120 s)
    so the first request against a new schema survives grammar compilation."""
    import boto3
    from botocore.config import Config
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(read_timeout=120, retries={"max_attempts": 2, "mode": "standard"}),
    )


# =========================================================================
# Local validation
# =========================================================================

# Keywords the Bedrock Structured Outputs subset rejects with HTTP 400
# (Bedrock user guide, "Get validated JSON results from models", Oct 2026).
UNSUPPORTED_KEYWORDS = ("minimum", "maximum", "multipleOf", "minLength", "maxLength")
MAX_OPTIONAL_PROPERTIES = 12
MAX_UNION_TYPED_PROPERTIES = 16

def validate_schema(schema: dict[str, Any]) -> list[str]:
    """Validate a schema locally and return a list of problems.

    Two layers of checking:

      1. Is it a legal JSON Schema Draft 2020-12 document? Bedrock returns
         HTTP 400 for an illegal schema, and catching that in a unit test is
         considerably cheaper than catching it in production.

      2. Does it follow the production conventions from Chapter 9 Section
         9.6? These are not JSON Schema requirements; they are the rules
         that keep downstream consumers honest.
    """
    problems: list[str] = []

    if _HAS_JSONSCHEMA:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            problems.append(f"invalid Draft 2020-12 schema: {exc.message}")
            return problems
    else:  # pragma: no cover
        problems.append(
            "jsonschema is not installed; skipped Draft 2020-12 structural check"
        )

    problems.extend(_check_conventions(schema, path="$"))
    problems.extend(check_bedrock_subset(schema))
    return problems


def check_bedrock_subset(schema: dict[str, Any]) -> list[str]:
    """Flag what Bedrock Structured Outputs would reject with HTTP 400.

    Draft 2020-12 allows all of these; Bedrock's subset does not. Checking
    here turns a 400 at request time (or after a ~90 s compile, for the
    optional-property limit) into a failing unit test.
    """
    problems: list[str] = []
    counts = {"optional": 0, "union": 0}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
            return
        for keyword in UNSUPPORTED_KEYWORDS:
            if keyword in node:
                problems.append(
                    f"{path}: '{keyword}' is not supported by Bedrock Structured "
                    f"Outputs (HTTP 400); enforce it in check_ranges instead"
                )
        if "minItems" in node and node["minItems"] not in (0, 1):
            problems.append(f"{path}: minItems must be 0 or 1 for Bedrock")
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            problems.append(f"{path}: external $ref is not supported by Bedrock")
        if isinstance(node.get("type"), list) or "anyOf" in node:
            counts["union"] += 1
        if node.get("type") == "object":
            props = node.get("properties", {}) or {}
            required = set(node.get("required", []) or [])
            counts["optional"] += len(set(props) - required)
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: Bedrock requires additionalProperties=false")
        for key, value in node.items():
            if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
                for name, sub in value.items():
                    walk(sub, f"{path}.{name}")
            elif key in ("items", "anyOf", "allOf"):
                walk(value, f"{path}.{key}")

    walk(schema, "$")
    if counts["optional"] > MAX_OPTIONAL_PROPERTIES:
        problems.append(
            f"$: {counts['optional']} optional properties; Bedrock allows "
            f"{MAX_OPTIONAL_PROPERTIES} ('Schema is too complex')"
        )
    if counts["union"] > MAX_UNION_TYPED_PROPERTIES:
        problems.append(
            f"$: {counts['union']} union-typed properties; Bedrock allows "
            f"{MAX_UNION_TYPED_PROPERTIES}"
        )
    return problems


# =========================================================================
# Post-parse business checks
# =========================================================================
# Ranges and lengths the schema cannot carry. Run these after json.loads and
# before anything acts on the result. Each rule: (field, min, max, max_len).

RANGE_RULES: dict[str, list[tuple[str, Optional[float], Optional[float], Optional[int]]]] = {
    "ticket_triage": [
        ("confidence", 0.0, 1.0, None),
        ("summary", None, None, 140),
    ],
    "issue_refund": [
        ("amount_cents", 1, None, None),
    ],
}


def check_ranges(contract: str, payload: dict[str, Any]) -> list[str]:
    """Return the business-rule violations in a parsed payload.

    An empty list means the payload is within range. A non-empty list is a
    failure with a reason: emit a metric and route to a human; never coerce.
    """
    problems: list[str] = []
    for field_name, low, high, max_len in RANGE_RULES.get(contract, []):
        value = payload.get(field_name)
        if value is None:
            continue
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if low is not None and is_number and value < low:
            problems.append(f"{field_name}={value} is below {low}")
        if high is not None and is_number and value > high:
            problems.append(f"{field_name}={value} is above {high}")
        if max_len is not None and isinstance(value, str) and len(value) > max_len:
            problems.append(f"{field_name} has {len(value)} characters; max {max_len}")
    return problems


def _check_conventions(node: Any, path: str) -> list[str]:
    """Walk the schema and enforce the production conventions."""
    problems: list[str] = []
    if not isinstance(node, dict):
        return problems

    if node.get("type") == "object":
        if node.get("additionalProperties") is not False:
            problems.append(
                f"{path}: objects must set additionalProperties=false, "
                f"otherwise the model may add fields the contract does not cover"
            )
        properties = node.get("properties", {})
        required = set(node.get("required", []))
        missing = set(properties) - required
        if missing:
            problems.append(
                f"{path}: every property should be required (model optional "
                f"fields as nullable types instead). Not required: "
                f"{sorted(missing)}"
            )
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, dict) and "description" not in prop_schema:
                problems.append(
                    f"{path}.{prop_name}: missing description. Descriptions are "
                    f"how the schema steers semantics, not just shape."
                )
            problems.extend(_check_conventions(prop_schema, f"{path}.{prop_name}"))

    if node.get("type") == "array" and isinstance(node.get("items"), dict):
        problems.extend(_check_conventions(node["items"], f"{path}[]"))

    return problems


# =========================================================================
# Compatibility checking between versions
# =========================================================================

def _types_equal(old_type: Any, new_type: Any) -> bool:
    """Compare a JSON Schema `type` value order-insensitively.

    `type` may be a scalar ("string") or a list (["string", "null"]). Two
    lists with the same members in a different order are the same type
    constraint and must not be reported as a breaking change. When either
    side is a scalar, fall back to direct comparison.
    """
    if isinstance(old_type, list) and isinstance(new_type, list):
        return set(old_type) == set(new_type)
    return old_type == new_type


def _compare_node(
    old_prop: dict[str, Any],
    new_prop: dict[str, Any],
    path: str,
    breaking: list[str],
    compatible: list[str],
) -> None:
    """Compare a single property's schema (type, enum) and recurse into any
    nested object properties or array items it contains.

    `path` is the diagnostic location of this property, e.g. "confidence"
    at the top level or "entities[].source_quote" for a property nested
    inside an array item.
    """
    old_type = old_prop.get("type")
    new_type = new_prop.get("type")
    if not _types_equal(old_type, new_type):
        breaking.append(f"property '{path}' changed type: {old_type} -> {new_type}")

    old_enum = set(old_prop.get("enum", []) or [])
    new_enum = set(new_prop.get("enum", []) or [])
    if old_enum and new_enum:
        removed = old_enum - new_enum
        added = new_enum - old_enum
        if removed:
            breaking.append(f"property '{path}' removed enum values: {sorted(removed)}")
        if added:
            # The model can now emit a value that consumers' match statements
            # and lookup tables have never seen: breaking for every consumer.
            breaking.append(f"property '{path}' added enum values: {sorted(added)}")
    elif old_enum and not new_enum:
        # The enum constraint was removed entirely: the model may now emit any
        # string, which is the widest form of adding enum values.
        breaking.append(f"removed enum constraint on '{path}' (widening, breaking for consumers)")

    # Recurse into a nested object's own properties (e.g. a property whose
    # schema is itself `type: "object"` with `properties`).
    if isinstance(old_prop.get("properties"), dict) or isinstance(new_prop.get("properties"), dict):
        _compare_properties(old_prop, new_prop, path, breaking, compatible)

    # Recurse into an array's `items` sub-schema. This is what makes a
    # breaking change inside `entities[].source_quote` visible instead of
    # invisible: the top-level `entities` property itself never changes
    # type or shape, only what its items require.
    old_items = old_prop.get("items")
    new_items = new_prop.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        _compare_node(old_items, new_items, f"{path}[]", breaking, compatible)


def _compare_properties(
    old_node: dict[str, Any],
    new_node: dict[str, Any],
    path: str,
    breaking: list[str],
    compatible: list[str],
) -> None:
    """Compare the `properties` and `required` of two object schema nodes.

    Used both for the top-level schema (`path == ""`) and for any nested
    object encountered while recursing (`path` is the dotted/bracketed
    location of that object, e.g. "entities[]").
    """
    old_props = old_node.get("properties", {}) or {}
    new_props = new_node.get("properties", {}) or {}
    old_required = set(old_node.get("required", []) or [])
    new_required = set(new_node.get("required", []) or [])

    for name, old_prop in old_props.items():
        prop_path = f"{path}.{name}" if path else name

        if name not in new_props:
            breaking.append(f"removed property '{prop_path}'")
            continue

        new_prop = new_props[name]
        _compare_node(old_prop, new_prop, prop_path, breaking, compatible)

        if name not in old_required and name in new_required:
            breaking.append(
                f"property '{prop_path}' changed from optional to required"
            )

        # The mirror case, and the one that is easy to miss: a property that
        # was guaranteed present is now optional. Producers are unaffected,
        # but every consumer written against the old schema may have treated
        # the field as always-there. That is a contract change downstream
        # code can crash on, so it is breaking in the same sense as removing
        # the property outright — just quieter.
        if name in old_required and name not in new_required:
            breaking.append(
                f"property '{prop_path}' changed from required to optional"
            )

    for name, new_prop in new_props.items():
        if name not in old_props:
            prop_path = f"{path}.{name}" if path else name
            if name in new_required:
                breaking.append(f"added required property '{prop_path}'")
            else:
                compatible.append(f"added optional property '{prop_path}'")


def compare_versions(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    """Classify the difference between two schema versions.

    Returns a dict with 'breaking' and 'compatible' change lists.

    Breaking changes are ones that can invalidate a downstream consumer:
    removing a property, any change to an enum (narrowing breaks the prompts
    and tests that produce the removed value; widening breaks every consumer
    that matches on the enum), changing a type, adding a
    required property that consumers do not know about, an existing
    optional property becoming required, or a required property becoming
    optional (consumers may have assumed it was always present). Run this
    in CI on every schema change and require an explicit version bump plus
    a consumer review when the breaking list is non-empty.

    The comparison recurses into nested schema structures — an array
    property's `items` and an object property's `properties` — so a
    breaking change several levels deep (e.g. a required field removed
    from an array item's schema) is reported with a path-qualified message
    like "entities[].source_quote" rather than going undetected.
    """
    breaking: list[str] = []
    compatible: list[str] = []

    _compare_properties(old, new, "", breaking, compatible)

    return {"breaking": breaking, "compatible": compatible}


# =========================================================================
# Module-level registry, populated from the schema library
# =========================================================================

def build_default_registry() -> SchemaRegistry:
    """Build the registry used by the Chapter 9 examples."""
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from schemas.production_schemas import (  # noqa: E402
        TRIAGE_SCHEMA,
        EXTRACTION_SCHEMA,
        DECISION_SCHEMA,
    )

    registry = SchemaRegistry()
    registry.register("ticket_triage", 1, TRIAGE_SCHEMA,
                      "Triage classification for an inbound support ticket.")
    registry.register("entity_extraction", 1, EXTRACTION_SCHEMA,
                      "Entity extraction with verbatim source provenance.")
    registry.register("policy_decision", 1, DECISION_SCHEMA,
                      "Policy decision with an explicit abstention path.")
    return registry


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    reg = build_default_registry()
    for schema_name in reg.names():
        entry = reg.latest(schema_name)
        print(f"{entry.qualified_name:<26} {entry.description}")
