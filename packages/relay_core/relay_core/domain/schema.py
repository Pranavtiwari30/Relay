"""JSON Schema validation over a deliberately restricted keyword subset.

Restrictions and why they exist:

* ``$ref``/``$id``/``$defs`` are rejected and the validator is given an empty
  registry, so validating a payload can never trigger a network fetch (SSRF).
* ``pattern``/``patternProperties`` are rejected: Python's backtracking regex
  engine would let an inbound payload trigger catastrophic backtracking.
* Error messages are built here from the schema, never from payload values, so
  sensitive data cannot leak into stored failure details or API responses.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from referencing import Registry

from relay_core.errors import Issue

MAX_SCHEMA_BYTES: Final = 65_536
MAX_SCHEMA_DEPTH: Final = 12
MAX_SCHEMA_NODES: Final = 2_000
MAX_REPORTED_ERRORS: Final = 25

_ANNOTATIONS = frozenset({"$schema", "title", "description", "default", "examples", "format"})
_ASSERTIONS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)
_SUBSCHEMA_MAPS = frozenset({"properties"})
_SUBSCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf"})
_SUBSCHEMA_SINGLE = frozenset({"items", "additionalProperties"})
ALLOWED_KEYWORDS: Final = (
    _ANNOTATIONS | _ASSERTIONS | _SUBSCHEMA_MAPS | _SUBSCHEMA_LISTS | _SUBSCHEMA_SINGLE
)


def check_schema_definition(schema: Any, *, location: str) -> list[Issue]:
    """Validate that ``schema`` is a usable schema within the supported subset."""
    if not isinstance(schema, dict):
        return [Issue("invalid_schema", location, "schema must be a JSON object")]
    try:
        size = len(json.dumps(schema))
    except (TypeError, ValueError):
        return [Issue("invalid_schema", location, "schema is not serialisable as JSON")]
    if size > MAX_SCHEMA_BYTES:
        return [Issue("schema_too_large", location, f"schema exceeds {MAX_SCHEMA_BYTES} bytes")]

    issues: list[Issue] = []
    nodes = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SCHEMA_NODES or len(issues) >= MAX_REPORTED_ERRORS:
            return
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            issues.append(Issue("invalid_schema", path, "subschema must be an object or boolean"))
            return
        if depth > MAX_SCHEMA_DEPTH:
            issues.append(
                Issue("schema_too_deep", path, f"schema nests deeper than {MAX_SCHEMA_DEPTH}")
            )
            return
        for keyword, value in node.items():
            if keyword not in ALLOWED_KEYWORDS:
                issues.append(
                    Issue(
                        "unsupported_schema_keyword",
                        f"{path}.{keyword}",
                        f"'{keyword}' is not supported; allowed keywords are: "
                        + ", ".join(sorted(ALLOWED_KEYWORDS - _ANNOTATIONS)),
                    )
                )
            elif keyword in _SUBSCHEMA_MAPS:
                if isinstance(value, dict):
                    for name, sub in value.items():
                        walk(sub, f"{path}.{keyword}.{name}", depth + 1)
            elif keyword in _SUBSCHEMA_LISTS:
                if isinstance(value, list):
                    for index, sub in enumerate(value):
                        walk(sub, f"{path}.{keyword}[{index}]", depth + 1)
            elif keyword in _SUBSCHEMA_SINGLE:
                walk(value, f"{path}.{keyword}", depth + 1)

    walk(schema, location, 0)
    if nodes > MAX_SCHEMA_NODES:
        issues.append(
            Issue("schema_too_large", location, f"schema has more than {MAX_SCHEMA_NODES} nodes")
        )
    if issues:
        return issues
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        where = ".".join(str(part) for part in exc.absolute_path)
        issues.append(
            Issue(
                "invalid_schema",
                f"{location}.{where}" if where else location,
                f"not a valid JSON Schema: {exc.message[:200]}",
            )
        )
    return issues


@lru_cache(maxsize=256)
def _compiled(canonical: str) -> Draft202012Validator:
    # An empty registry has no retriever: remote references cannot be resolved.
    return Draft202012Validator(json.loads(canonical), registry=Registry())


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _format_location(parts: Any) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "$"


def _describe(error: JsonSchemaValidationError) -> str:
    keyword, expected = error.validator, error.validator_value
    if keyword == "type":
        wanted = " or ".join(expected) if isinstance(expected, list) else str(expected)
        return f"expected {wanted}, got {_type_of(error.instance)}"
    if keyword == "enum":
        return "value is not one of the allowed values"
    if keyword == "const":
        return "value does not equal the required constant"
    if keyword in ("minimum", "exclusiveMinimum"):
        return f"must be {'>' if keyword.startswith('exclusive') else '>='} {expected}"
    if keyword in ("maximum", "exclusiveMaximum"):
        return f"must be {'<' if keyword.startswith('exclusive') else '<='} {expected}"
    if keyword == "minLength":
        return f"must be at least {expected} characters long"
    if keyword == "maxLength":
        return f"must be at most {expected} characters long"
    if keyword == "minItems":
        return f"must contain at least {expected} items"
    if keyword == "maxItems":
        return f"must contain at most {expected} items"
    if keyword == "additionalProperties":
        instance = error.instance if isinstance(error.instance, dict) else {}
        schema = error.schema if isinstance(error.schema, dict) else {}
        extra = sorted(set(instance) - set(schema.get("properties", {})))[:10]
        return "unexpected field(s): " + ", ".join(str(name)[:64] for name in extra)
    if keyword in ("anyOf", "oneOf", "allOf"):
        return f"does not satisfy the '{keyword}' alternatives"
    return f"failed the '{keyword}' check"


def validate_instance(schema: dict[str, Any], instance: Any) -> list[Issue]:
    """Validate ``instance`` and return at most ``MAX_REPORTED_ERRORS`` issues."""
    validator = _compiled(json.dumps(schema, sort_keys=True))
    issues: list[Issue] = []
    for error in validator.iter_errors(instance):
        if len(issues) >= MAX_REPORTED_ERRORS:
            break
        base = _format_location(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, dict):
            for name in error.validator_value:
                if name not in error.instance:
                    path = name if base == "$" else f"{base}.{name}"
                    issues.append(
                        Issue("required_field_missing", path, "required field is missing")
                    )
            continue
        issues.append(Issue(f"schema_{error.validator}", base, _describe(error)))
    return issues[:MAX_REPORTED_ERRORS]
