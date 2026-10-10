from __future__ import annotations

import copy
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from relay_core.domain.transform.convert import ConversionError, ConvertOp, convert
from relay_core.domain.transform.interpreter import apply_mappings, evaluate_filters
from relay_core.domain.transform.models import FilterRule, IntegrationConfig, Mapping
from relay_core.domain.transform.paths import (
    MISSING,
    PathConflictError,
    PathSyntaxError,
    format_path,
    get_path,
    parse_path,
    set_path,
)
from relay_core.domain.transform.pipeline import (
    PipelineStage,
    has_errors,
    parse_config,
    run_pipeline,
    validate_config,
)

ORDER = {
    "id": "ord_1001",
    "customer": {"name": "  Ada Lovelace ", "email": "ada@example.com"},
    "qty": "3",
    "unit_cost": "19.50",
    "created": "2026-03-01T10:15:00+02:00",
    "gift": "yes",
    "items": [{"sku": "kb-1", "qty": "2"}, {"sku": "ms-9", "qty": 1}],
    "status": "paid",
    "total": 58.5,
}


def m(**kwargs: Any) -> Mapping:
    return Mapping.model_validate(kwargs)


# --- paths -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a", ("a",)),
        ("a.b.c", ("a", "b", "c")),
        ("items[0].sku", ("items", 0, "sku")),
        ("a[1][2]", ("a", 1, 2)),
        ("order-id.@type", ("order-id", "@type")),
    ],
)
def test_parse_path_accepts_documented_grammar(path: str, expected: tuple[Any, ...]) -> None:
    assert parse_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".a",
        "a.",
        "a..b",
        "[0]",
        "a[",
        "a[x]",
        "a[-1]",
        "a[0]b",
        "a]b",
        "a[10000]",
        "a\x00b",
        ".".join(["k"] * 13),
        "k" * 129,
    ],
)
def test_parse_path_rejects_malformed_paths(path: str) -> None:
    with pytest.raises(PathSyntaxError):
        parse_path(path)


def test_target_paths_reject_indexes() -> None:
    with pytest.raises(PathSyntaxError):
        parse_path("lines[0].sku", allow_indexes=False)


def test_get_path_returns_missing_on_shape_mismatch() -> None:
    assert get_path(ORDER, parse_path("customer.name")) == "  Ada Lovelace "
    assert get_path(ORDER, parse_path("items[1].sku")) == "ms-9"
    assert get_path(ORDER, parse_path("items[5].sku")) is MISSING
    assert get_path(ORDER, parse_path("customer.name.first")) is MISSING
    assert get_path(ORDER, parse_path("qty[0]")) is MISSING


def test_set_path_builds_nested_objects_and_refuses_conflicts() -> None:
    out: dict[str, Any] = {}
    set_path(out, ("a", "b"), 1)
    set_path(out, ("a", "c"), 2)
    assert out == {"a": {"b": 1, "c": 2}}
    with pytest.raises(PathConflictError):
        set_path(out, ("a", "b"), 3)
    with pytest.raises(PathConflictError):
        set_path(out, ("a", "b", "deeper"), 3)


_KEY = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E, exclude_characters=".[]"),
    min_size=1,
    max_size=12,
)


@given(first=_KEY, rest=st.lists(st.one_of(_KEY, st.integers(0, 9999)), max_size=8))
def test_path_parse_format_round_trip(first: str, rest: list[Any]) -> None:
    segments = (first, *rest)
    assert parse_path(format_path(segments)) == segments


# --- conversions -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "op", "expected"),
    [
        (3, ConvertOp.STRING, "3"),
        (True, ConvertOp.STRING, "true"),
        (2.0, ConvertOp.STRING, "2"),
        ("42", ConvertOp.INTEGER, 42),
        (" -7 ", ConvertOp.INTEGER, -7),
        (5.0, ConvertOp.INTEGER, 5),
        ("19.50", ConvertOp.NUMBER, 19.5),
        ("7", ConvertOp.NUMBER, 7),
        ("1e3", ConvertOp.NUMBER, 1000.0),
        ("YES", ConvertOp.BOOLEAN, True),
        ("off", ConvertOp.BOOLEAN, False),
        (1, ConvertOp.BOOLEAN, True),
        ("2026-03-01T10:15:00+02:00", ConvertOp.DATETIME, "2026-03-01T08:15:00Z"),
        ("2026-03-01T10:15:00", ConvertOp.DATETIME, "2026-03-01T10:15:00Z"),
        ("2026-03-01T10:15:00.250Z", ConvertOp.DATETIME, "2026-03-01T10:15:00.250000Z"),
        (1_700_000_000, ConvertOp.DATETIME_FROM_EPOCH_SECONDS, "2023-11-14T22:13:20Z"),
        (1_700_000_000_000, ConvertOp.DATETIME_FROM_EPOCH_MILLIS, "2023-11-14T22:13:20Z"),
        ("2026-03-01", ConvertOp.DATE, "2026-03-01"),
        ("2026-03-01T23:30:00-05:00", ConvertOp.DATE, "2026-03-02"),
        ("abc", ConvertOp.UPPERCASE, "ABC"),
        ("ABC", ConvertOp.LOWERCASE, "abc"),
        ("  x ", ConvertOp.TRIM, "x"),
    ],
)
def test_convert_documented_cases(value: Any, op: ConvertOp, expected: Any) -> None:
    result = convert(value, op)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    ("value", "op"),
    [
        ({"a": 1}, ConvertOp.STRING),
        ([1], ConvertOp.STRING),
        ("3.5", ConvertOp.INTEGER),
        (3.5, ConvertOp.INTEGER),
        (True, ConvertOp.INTEGER),
        ("12abc", ConvertOp.INTEGER),
        ("9" * 30, ConvertOp.INTEGER),
        ("nan", ConvertOp.NUMBER),
        ("inf", ConvertOp.NUMBER),
        (float("inf"), ConvertOp.NUMBER),
        (False, ConvertOp.NUMBER),
        ("maybe", ConvertOp.BOOLEAN),
        (2, ConvertOp.BOOLEAN),
        ("yesterday", ConvertOp.DATETIME),
        (1700000000, ConvertOp.DATETIME),
        ("1700000000", ConvertOp.DATETIME_FROM_EPOCH_SECONDS),
        (1e30, ConvertOp.DATETIME_FROM_EPOCH_SECONDS),
        ("2026-13-40", ConvertOp.DATE),
        (5, ConvertOp.UPPERCASE),
        (None, ConvertOp.TRIM),
    ],
)
def test_convert_rejects_type_mismatches(value: Any, op: ConvertOp) -> None:
    with pytest.raises(ConversionError):
        convert(value, op)


def test_conversion_errors_do_not_echo_long_values() -> None:
    secret_like = "sk_live_" + "a" * 200
    with pytest.raises(ConversionError) as info:
        convert(secret_like, ConvertOp.INTEGER)
    assert len(str(info.value)) < 120


# --- mapping model ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "a"},
        {"target": "a", "source": "x", "const": 1},
        {"target": "a", "const": 1, "required": True},
        {"target": "a", "const": 1, "convert": "string"},
        {"target": "a", "source": "x", "required": True, "default": 1},
        {
            "target": "a",
            "source": "x",
            "convert": "string",
            "each": [{"target": "b", "source": "c"}],
        },
        {"target": "a", "source": "x", "each": []},
        {"target": "a", "source": "x", "surprise": 1},
        {"target": "a", "source": "x", "convert": "eval"},
    ],
)
def test_mapping_model_rejects_ambiguous_shapes(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError subclasses ValueError
        Mapping.model_validate(raw)


def test_null_const_and_default_survive_storage_round_trip() -> None:
    config = IntegrationConfig.model_validate(
        {
            "mappings": [
                {"target": "note", "const": None},
                {"target": "coupon", "source": "coupon", "default": None},
                {"target": "id", "source": "id", "required": True},
            ]
        }
    )
    stored = config.to_storage()
    assert stored["mappings"][0] == {"target": "note", "const": None}
    assert stored["mappings"][1] == {"target": "coupon", "source": "coupon", "default": None}
    assert stored["mappings"][2] == {"target": "id", "source": "id", "required": True}
    assert IntegrationConfig.model_validate(stored).to_storage() == stored


# --- interpreter -----------------------------------------------------------


def test_apply_mappings_renames_converts_defaults_and_nests() -> None:
    output, issues = apply_mappings(
        ORDER,
        [
            m(target="order_id", source="id", required=True),
            m(target="customer.name", source="customer.name", convert="trim"),
            m(target="quantity", source="qty", convert="integer"),
            m(target="unit_price", source="unit_cost", convert="number"),
            m(target="ordered_at", source="created", convert="datetime"),
            m(target="is_gift", source="gift", convert="boolean"),
            m(target="currency", source="currency", default="USD"),
            m(target="channel", const="vendor-x"),
            m(target="coupon", source="coupon"),
        ],
    )
    assert issues == []
    assert output == {
        "order_id": "ord_1001",
        "customer": {"name": "Ada Lovelace"},
        "quantity": 3,
        "unit_price": 19.5,
        "ordered_at": "2026-03-01T08:15:00Z",
        "is_gift": True,
        "currency": "USD",
        "channel": "vendor-x",
    }


def test_apply_mappings_each_maps_array_elements() -> None:
    output, issues = apply_mappings(
        ORDER,
        [
            m(
                target="lines",
                source="items",
                each=[
                    {"target": "sku", "source": "sku", "convert": "uppercase"},
                    {"target": "quantity", "source": "qty", "convert": "integer"},
                ],
            )
        ],
    )
    assert issues == []
    assert output == {"lines": [{"sku": "KB-1", "quantity": 2}, {"sku": "MS-9", "quantity": 1}]}


def test_apply_mappings_collects_every_issue_with_source_locations() -> None:
    payload = {"qty": "three", "items": [{"sku": "a", "qty": "x"}, "oops"], "id": None}
    _, issues = apply_mappings(
        payload,
        [
            m(target="order_id", source="id", required=True),
            m(target="customer", source="customer.name", required=True),
            m(target="quantity", source="qty", convert="integer"),
            m(
                target="lines",
                source="items",
                each=[{"target": "q", "source": "qty", "convert": "integer"}],
            ),
        ],
    )
    found = {(issue.code, issue.path) for issue in issues}
    assert found == {
        ("required_field_missing", "id"),
        ("required_field_missing", "customer.name"),
        ("conversion_failed", "qty"),
        ("conversion_failed", "items[0].qty"),
        ("type_mismatch", "items[1]"),
    }
    assert all(issue.message for issue in issues)


def test_apply_mappings_reports_non_array_each_and_target_conflicts() -> None:
    _, issues = apply_mappings(
        {"items": {"sku": 1}, "a": 1, "b": 2},
        [
            m(target="lines", source="items", each=[{"target": "sku", "source": "sku"}]),
            m(target="x", source="a"),
            m(target="x", source="b"),
        ],
    )
    assert [issue.code for issue in issues] == ["type_mismatch", "target_conflict"]


def test_apply_mappings_never_mutates_the_payload() -> None:
    before = copy.deepcopy(ORDER)
    apply_mappings(
        ORDER, [m(target="lines", source="items", each=[{"target": "s", "source": "sku"}])]
    )
    assert before == ORDER


_JSON = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-(10**6), 10**6), st.text(max_size=8)),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.sampled_from(["a", "b", "c", "qty", "items"]), children, max_size=4),
    ),
    max_leaves=20,
)
_MAPPINGS = [
    m(target="one", source="a"),
    m(target="two.n", source="b.c", convert="integer"),
    m(target="three", source="qty", default=0),
    m(target="four", source="items", each=[{"target": "v", "source": "a", "convert": "string"}]),
]


@given(payload=st.dictionaries(st.sampled_from(["a", "b", "qty", "items"]), _JSON, max_size=4))
def test_interpreter_is_total_and_deterministic(payload: dict[str, Any]) -> None:
    """Arbitrary JSON never raises, and the same input always gives the same result."""
    first = apply_mappings(payload, _MAPPINGS)
    second = apply_mappings(copy.deepcopy(payload), _MAPPINGS)
    assert first == second


# --- filters ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({"path": "status", "op": "eq", "value": "paid"}, True),
        ({"path": "status", "op": "ne", "value": "paid"}, False),
        ({"path": "status", "op": "in", "value": ["paid", "shipped"]}, True),
        ({"path": "status", "op": "not_in", "value": ["void"]}, True),
        ({"path": "missing", "op": "not_in", "value": ["void"]}, True),
        ({"path": "missing", "op": "ne", "value": 1}, True),
        ({"path": "missing", "op": "eq", "value": 1}, False),
        ({"path": "customer.email", "op": "exists"}, True),
        ({"path": "customer.phone", "op": "not_exists"}, True),
        ({"path": "total", "op": "gt", "value": 50}, True),
        ({"path": "total", "op": "lte", "value": 50}, False),
        ({"path": "total", "op": "gte", "value": "50"}, False),  # no cross-type ordering
        ({"path": "customer.email", "op": "contains", "value": "@example.com"}, True),
        ({"path": "items", "op": "contains", "value": 5}, False),
        ({"path": "flag", "op": "eq", "value": 1}, False),  # true is not 1
    ],
)
def test_filter_semantics(rule: dict[str, Any], expected: bool) -> None:
    payload = {**ORDER, "flag": True}
    matched, reason = evaluate_filters(payload, [FilterRule.model_validate(rule)])
    assert matched is expected
    assert (reason is None) is expected


@pytest.mark.parametrize(
    "raw",
    [
        {"path": "a", "op": "eq"},
        {"path": "a", "op": "exists", "value": 1},
        {"path": "a", "op": "in", "value": "paid"},
        {"path": "a", "op": "in", "value": []},
        {"path": "a", "op": "gt", "value": True},
        {"path": "a", "op": "eq", "value": {"x": 1}},
        {"path": "a", "op": "regex", "value": ".*"},
    ],
)
def test_filter_model_rejects_unsupported_rules(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        FilterRule.model_validate(raw)


# --- pipeline --------------------------------------------------------------

CONFIG = IntegrationConfig.model_validate(
    {
        "input_schema": {
            "type": "object",
            "required": ["id", "qty"],
            "properties": {"id": {"type": "string"}, "qty": {"type": ["string", "integer"]}},
        },
        "filters": [{"path": "status", "op": "eq", "value": "paid"}],
        "mappings": [
            {"target": "order_id", "source": "id", "required": True},
            {"target": "quantity", "source": "qty", "convert": "integer"},
        ],
        "output_schema": {
            "type": "object",
            "required": ["order_id", "quantity"],
            "additionalProperties": False,
            "properties": {
                "order_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
            },
        },
    }
)


def test_pipeline_ready() -> None:
    result = run_pipeline(ORDER, CONFIG)
    assert result.kind == "ready"
    assert result.output == {"order_id": "ord_1001", "quantity": 3}


def test_pipeline_input_schema_failure_is_actionable_and_stops_early() -> None:
    result = run_pipeline({"status": "paid", "qty": [1]}, CONFIG)
    assert result.kind == "validation_failed"
    assert result.stage is PipelineStage.INPUT_SCHEMA
    assert {(i.code, i.path) for i in result.issues} == {
        ("required_field_missing", "id"),
        ("schema_type", "qty"),
    }
    assert result.output is None


def test_pipeline_filtered_events_are_not_transformed() -> None:
    result = run_pipeline({**ORDER, "status": "void"}, CONFIG)
    assert result.kind == "filtered"
    assert result.filter_reason is not None
    assert "status" in result.filter_reason


def test_pipeline_transform_failure() -> None:
    result = run_pipeline({**ORDER, "qty": "lots"}, CONFIG)
    assert (result.kind, result.stage) == ("validation_failed", PipelineStage.TRANSFORM)
    assert result.issues[0].code == "conversion_failed"


def test_pipeline_output_schema_failure() -> None:
    result = run_pipeline({**ORDER, "qty": 0}, CONFIG)
    assert (result.kind, result.stage) == ("validation_failed", PipelineStage.OUTPUT_SCHEMA)
    assert result.issues[0].path == "quantity"


def test_pipeline_rejects_non_object_payload() -> None:
    assert run_pipeline([1, 2], CONFIG).kind == "validation_failed"


def test_pipeline_passthrough_delivers_payload_unchanged() -> None:
    config = IntegrationConfig.model_validate({"passthrough": True})
    result = run_pipeline(ORDER, config)
    assert result.kind == "ready"
    assert result.output == ORDER


# --- static configuration validation ---------------------------------------


def test_valid_config_has_no_errors() -> None:
    assert not has_errors(validate_config(CONFIG))


def test_validate_config_reports_actionable_problems() -> None:
    config = IntegrationConfig.model_validate(
        {
            "event_types": [""],
            "filters": [{"path": "a..b", "op": "exists"}],
            "input_schema": {"type": "object", "properties": {"x": {"pattern": "^(a+)+$"}}},
            "mappings": [
                {"target": "a", "source": "x"},
                {"target": "a.b", "source": "y"},
                {"target": "bad[0]", "source": "z"},
                {"target": "extra", "source": "q.."},
            ],
            "output_schema": {
                "type": "object",
                "required": ["sku"],
                "additionalProperties": False,
                "properties": {"sku": {"type": "string"}, "a": {}},
            },
        }
    )
    codes = {(issue.code, issue.path) for issue in validate_config(config)}
    assert ("invalid_event_type", "event_types[0]") in codes
    assert ("invalid_path", "filters[0].path") in codes
    assert ("unsupported_schema_keyword", "input_schema.properties.x.pattern") in codes
    assert ("target_conflict", "mappings[0].target") in codes
    assert ("invalid_path", "mappings[2].target") in codes
    assert ("invalid_path", "mappings[3].source") in codes
    assert ("unmapped_required_field", "output_schema.required.sku") in codes
    assert ("unexpected_target", "mappings") in codes


def test_validate_config_requires_mappings_or_passthrough_but_not_both() -> None:
    empty = validate_config(IntegrationConfig())
    assert any(i.code == "invalid_config" and i.path == "mappings" for i in empty)
    both = IntegrationConfig.model_validate(
        {"passthrough": True, "mappings": [{"target": "a", "source": "a"}]}
    )
    assert any(i.code == "invalid_config" for i in validate_config(both))


def test_validate_config_bounds_each_nesting() -> None:
    nested: dict[str, Any] = {"target": "leaf", "source": "leaf"}
    for _ in range(4):
        nested = {"target": "level", "source": "level", "each": [nested]}
    config = IntegrationConfig.model_validate({"mappings": [nested]})
    assert any(issue.code == "each_too_deep" for issue in validate_config(config))


def test_missing_schemas_are_warnings_not_errors() -> None:
    config = IntegrationConfig.model_validate({"mappings": [{"target": "a", "source": "a"}]})
    issues = validate_config(config)
    assert {i.code for i in issues} == {"no_input_schema", "no_output_schema"}
    assert not has_errors(issues)


def test_parse_config_turns_pydantic_errors_into_located_issues() -> None:
    config, issues = parse_config(
        {"mappings": [{"target": "a"}], "retry": {"max_attempts": 99}, "unknown": True}
    )
    assert config is None
    by_path = {issue.path: issue.message for issue in issues}
    assert "mappings[0]" in by_path
    assert "exactly one of" in by_path["mappings[0]"]
    assert "retry.max_attempts" in by_path
    assert "unknown" in by_path
