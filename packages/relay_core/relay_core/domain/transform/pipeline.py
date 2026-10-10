"""The deterministic processing pipeline and static configuration checks.

``run_pipeline`` is the one definition of "what happens to a payload":
input validation -> filters -> mapping -> output validation. Ingestion, the
worker, the editor preview and the read-only ``validate_proposed_mapping``
tool all call it, so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError

from relay_core.domain.schema import check_schema_definition, validate_instance
from relay_core.domain.transform.interpreter import apply_mappings, evaluate_filters
from relay_core.domain.transform.models import MAX_EACH_DEPTH, IntegrationConfig, Mapping
from relay_core.domain.transform.paths import PathSyntaxError, Segment, parse_path
from relay_core.errors import Issue


class PipelineStage(StrEnum):
    INPUT_SCHEMA = "input_schema"
    TRANSFORM = "transform"
    OUTPUT_SCHEMA = "output_schema"


@dataclass(frozen=True)
class PipelineResult:
    kind: Literal["ready", "validation_failed", "filtered"]
    stage: PipelineStage | None = None
    issues: tuple[Issue, ...] = ()
    output: dict[str, Any] | None = None
    filter_reason: str | None = None


def validate_input(payload: Any, config: IntegrationConfig) -> list[Issue]:
    if not isinstance(payload, dict):
        return [Issue("schema_type", "$", "payload must be a JSON object")]
    if config.input_schema is None:
        return []
    return validate_instance(config.input_schema, payload)


def run_pipeline(payload: Any, config: IntegrationConfig) -> PipelineResult:
    issues = validate_input(payload, config)
    if issues:
        return PipelineResult("validation_failed", PipelineStage.INPUT_SCHEMA, tuple(issues))

    matched, reason = evaluate_filters(payload, config.filters)
    if not matched:
        return PipelineResult("filtered", filter_reason=reason)

    if config.passthrough:
        output: dict[str, Any] = payload
    else:
        output, issues = apply_mappings(payload, config.mappings)
        if issues:
            return PipelineResult("validation_failed", PipelineStage.TRANSFORM, tuple(issues))

    if config.output_schema is not None:
        issues = validate_instance(config.output_schema, output)
        if issues:
            return PipelineResult(
                "validation_failed", PipelineStage.OUTPUT_SCHEMA, tuple(issues), output=output
            )
    return PipelineResult("ready", output=output)


def parse_config(raw: Any) -> tuple[IntegrationConfig | None, list[Issue]]:
    """Structural validation with actionable locations instead of a raw pydantic dump."""
    try:
        return IntegrationConfig.model_validate(raw), []
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_url=False, include_input=False, include_context=False)[:50]:
            location = ""
            for part in error["loc"]:
                location += (
                    f"[{part}]"
                    if isinstance(part, int)
                    else (f".{part}" if location else str(part))
                )
            message = error["msg"]
            if message.startswith("Value error, "):
                message = message.removeprefix("Value error, ")
            issues.append(Issue("invalid_config", location or "$", message))
        return None, issues


def _check_mappings(
    mappings: list[Mapping], location: str, depth: int, issues: list[Issue]
) -> list[tuple[Segment, ...]]:
    targets: list[tuple[tuple[Segment, ...], str]] = []
    for index, mapping in enumerate(mappings):
        here = f"{location}[{index}]"
        try:
            target = parse_path(mapping.target, allow_indexes=False)
            targets.append((target, here))
        except PathSyntaxError as exc:
            issues.append(Issue("invalid_path", f"{here}.target", str(exc)))
        if mapping.source is not None:
            try:
                parse_path(mapping.source)
            except PathSyntaxError as exc:
                issues.append(Issue("invalid_path", f"{here}.source", str(exc)))
        if mapping.each is not None:
            if depth + 1 > MAX_EACH_DEPTH:
                issues.append(
                    Issue(
                        "each_too_deep",
                        f"{here}.each",
                        f"'each' may be nested at most {MAX_EACH_DEPTH} levels",
                    )
                )
            else:
                _check_mappings(mapping.each, f"{here}.each", depth + 1, issues)
    for i, (first, first_at) in enumerate(targets):
        for second, _ in targets[i + 1 :]:
            shorter, longer = sorted((first, second), key=len)
            if longer[: len(shorter)] == shorter:
                issues.append(
                    Issue(
                        "target_conflict",
                        f"{first_at}.target",
                        f"target '{mappings_target(first)}' conflicts with '{mappings_target(second)}'",
                    )
                )
    return [target for target, _ in targets]


def mappings_target(segments: tuple[Segment, ...]) -> str:
    return ".".join(str(part) for part in segments)


def validate_config(config: IntegrationConfig) -> list[Issue]:
    """Static checks run before a configuration can be activated.

    Errors block activation; warnings are advisory. Checks are static: they
    prove the configuration is well-formed, not that every payload will pass.
    """
    issues: list[Issue] = []
    for index, name in enumerate(config.event_types):
        if not name or len(name) > 128:
            issues.append(
                Issue("invalid_event_type", f"event_types[{index}]", "must be 1 to 128 characters")
            )
    for index, rule in enumerate(config.filters):
        try:
            parse_path(rule.path)
        except PathSyntaxError as exc:
            issues.append(Issue("invalid_path", f"filters[{index}].path", str(exc)))
    for name, schema in (
        ("input_schema", config.input_schema),
        ("output_schema", config.output_schema),
    ):
        if schema is not None:
            issues.extend(check_schema_definition(schema, location=name))

    if config.passthrough and config.mappings:
        issues.append(
            Issue("invalid_config", "mappings", "remove mappings or turn off passthrough; not both")
        )
    if not config.passthrough and not config.mappings:
        issues.append(
            Issue("invalid_config", "mappings", "add at least one mapping or enable passthrough")
        )
    targets = _check_mappings(config.mappings, "mappings", 0, issues)

    out = config.output_schema
    if isinstance(out, dict) and not config.passthrough and config.mappings:
        produced = {str(target[0]) for target in targets if target}
        required = out.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in produced:
                    issues.append(
                        Issue(
                            "unmapped_required_field",
                            f"output_schema.required.{name}",
                            f"output schema requires '{name}' but no mapping produces it",
                        )
                    )
        properties = out.get("properties")
        if out.get("additionalProperties") is False and isinstance(properties, dict):
            for name in sorted(produced - set(properties)):
                issues.append(
                    Issue(
                        "unexpected_target",
                        "mappings",
                        f"mapping target '{name}' is not allowed by the output schema",
                    )
                )
    if config.input_schema is None:
        issues.append(
            Issue(
                "no_input_schema",
                "input_schema",
                "no input schema: inbound payloads are only checked by the mappings",
                severity="warning",
            )
        )
    if config.output_schema is None:
        issues.append(
            Issue(
                "no_output_schema",
                "output_schema",
                "no output schema: transformed payloads are delivered without a contract check",
                severity="warning",
            )
        )
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)
