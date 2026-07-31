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
    - Warm-up support for Bedrock's 24-hour grammar cache

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
        """The name passed to Bedrock. Versioned so the grammar cache key
        changes when the schema changes — two different schemas must never
        share a name."""
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

        Bedrock compiles each schema into a grammar and caches it for roughly
        24 hours. The first request against a new schema pays the compilation
        cost. On a deploy that introduces or changes schemas, that cost lands
        on whichever user happens to arrive first.

        Calling this at startup — with a trivial prompt — moves the cost to
        deploy time. Failures here are not fatal; log and continue.
        """
        results: dict[str, str] = {}
        for schema_name in (names or self.names()):
            try:
                bedrock_client.converse(
                    modelId=model_id,
                    messages=[{"role": "user", "content": [{"text": "warm"}]}],
                    inferenceConfig={"maxTokens": 64, "temperature": 0.0},
                    outputConfig=self.output_config(schema_name),
                )
                results[schema_name] = "warmed"
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Cache warm failed for %s: %s", schema_name, exc)
                results[schema_name] = f"failed: {exc}"
        return results


# =========================================================================
# Local validation
# =========================================================================

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
            compatible.append(f"property '{path}' added enum values: {sorted(added)}")
    elif old_enum and not new_enum:
        # The enum constraint was removed entirely rather than narrowed —
        # every value the old enum allowed is still allowed, so this is a
        # widening, not a breaking change.
        compatible.append(f"removed enum constraint on '{path}' (widening, compatible)")

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
    removing a property, narrowing an enum, changing a type, adding a
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
