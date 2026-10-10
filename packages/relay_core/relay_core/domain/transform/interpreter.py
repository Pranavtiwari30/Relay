"""Interpreter for the transformation DSL.

Pure functions over JSON values: no I/O, no clock, no randomness. The same
input and configuration always produce the same output, which is what makes
replay and ``validate_proposed_mapping`` trustworthy.

Cost is O(mappings x path depth), plus O(items) for an ``each`` mapping; every
factor is capped (see ``models.py`` and ``paths.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Final

from relay_core.domain.transform.convert import ConversionError, convert
from relay_core.domain.transform.models import MAX_EACH_ITEMS, FilterOp, FilterRule, Mapping
from relay_core.domain.transform.paths import (
    MISSING,
    PathConflictError,
    PathSyntaxError,
    Segment,
    get_path,
    parse_path,
    set_path,
)
from relay_core.errors import Issue

MAX_ISSUES: Final = 50


@lru_cache(maxsize=4096)
def _source(path: str) -> tuple[Segment, ...]:
    return parse_path(path)


@lru_cache(maxsize=4096)
def _target(path: str) -> tuple[Segment, ...]:
    return parse_path(path, allow_indexes=False)


def apply_mappings(
    payload: Any, mappings: Sequence[Mapping], *, source_prefix: str = ""
) -> tuple[dict[str, Any], list[Issue]]:
    """Build the output object. Returns ``(output, issues)``; output is partial if issues exist."""
    output: dict[str, Any] = {}
    issues: list[Issue] = []
    for mapping in mappings:
        if len(issues) >= MAX_ISSUES:
            break
        where = f"{source_prefix}{mapping.source}" if mapping.source else mapping.target
        try:
            value = _resolve(payload, mapping, where, issues)
        except PathSyntaxError as exc:
            issues.append(Issue("invalid_path", where, str(exc)))
            continue
        if value is MISSING:
            continue
        try:
            set_path(output, _target(mapping.target), value)
        except (PathSyntaxError, PathConflictError) as exc:
            issues.append(Issue("target_conflict", mapping.target, str(exc)))
    return output, issues[:MAX_ISSUES]


def _resolve(payload: Any, mapping: Mapping, where: str, issues: list[Issue]) -> Any:
    if mapping.has_const:
        return mapping.const
    assert mapping.source is not None  # guaranteed by Mapping validation
    value = get_path(payload, _source(mapping.source))
    if value is MISSING or value is None:
        # Absent and null are treated alike: a default fills either.
        if mapping.has_default:
            return mapping.default
        if mapping.required:
            issues.append(
                Issue(
                    "required_field_missing",
                    where,
                    f"required source field is {'null' if value is None else 'missing'} "
                    f"(needed for '{mapping.target}')",
                )
            )
        return MISSING
    if mapping.each is not None:
        return _apply_each(value, mapping, where, issues)
    if mapping.convert is not None:
        try:
            return convert(value, mapping.convert)
        except ConversionError as exc:
            issues.append(Issue("conversion_failed", where, f"{exc} (for '{mapping.target}')"))
            return MISSING
    return value


def _apply_each(value: Any, mapping: Mapping, where: str, issues: list[Issue]) -> Any:
    assert mapping.each is not None
    if not isinstance(value, list):
        issues.append(Issue("type_mismatch", where, "expected an array to apply 'each' to"))
        return MISSING
    if len(value) > MAX_EACH_ITEMS:
        issues.append(
            Issue("array_too_large", where, f"array has more than {MAX_EACH_ITEMS} items")
        )
        return MISSING
    items: list[dict[str, Any]] = []
    for index, element in enumerate(value):
        if len(issues) >= MAX_ISSUES:
            break
        if not isinstance(element, dict):
            issues.append(Issue("type_mismatch", f"{where}[{index}]", "expected an object"))
            continue
        item, item_issues = apply_mappings(
            element, mapping.each, source_prefix=f"{where}[{index}]."
        )
        issues.extend(item_issues)
        items.append(item)
    return items


def _same_json_type(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return True
    return type(a) is type(b)


def _json_equal(a: Any, b: Any) -> bool:
    # JSON semantics: true is not 1, and 1 equals 1.0.
    return _same_json_type(a, b) and bool(a == b)


def _ordered(a: Any, b: Any, op: FilterOp) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or not _same_json_type(a, b):
        return False
    if not isinstance(a, int | float | str):
        return False
    if op is FilterOp.GT:
        return bool(a > b)
    if op is FilterOp.GTE:
        return bool(a >= b)
    if op is FilterOp.LT:
        return bool(a < b)
    return bool(a <= b)


def _matches(value: Any, rule: FilterRule) -> bool:
    op, expected = rule.op, rule.value
    present = value is not MISSING
    if op is FilterOp.EXISTS:
        return present
    if op is FilterOp.NOT_EXISTS:
        return not present
    if op is FilterOp.NE:
        return not present or not _json_equal(value, expected)
    if op is FilterOp.NOT_IN:
        return not present or not any(_json_equal(value, item) for item in expected)
    if not present:
        return False
    if op is FilterOp.EQ:
        return _json_equal(value, expected)
    if op is FilterOp.IN:
        return any(_json_equal(value, item) for item in expected)
    if op is FilterOp.CONTAINS:
        if isinstance(value, str):
            return isinstance(expected, str) and expected in value
        if isinstance(value, list):
            return any(_json_equal(item, expected) for item in value)
        return False
    return _ordered(value, expected, op)


def evaluate_filters(payload: Any, filters: Sequence[FilterRule]) -> tuple[bool, str | None]:
    """Return ``(matched, reason)``. ``reason`` names the first rule that did not match."""
    for rule in filters:
        try:
            value = get_path(payload, _source(rule.path))
        except PathSyntaxError:
            return False, f"filter path '{rule.path}' is invalid"
        if not _matches(value, rule):
            return False, f"filter '{rule.path} {rule.op.value}' did not match"
    return True, None
