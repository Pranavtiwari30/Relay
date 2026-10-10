"""Declarative integration configuration: the transformation DSL as data.

A configuration is plain JSON validated into these models. There is no
expression language: every capability is an enumerated operation interpreted
by ``interpreter.py``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer
from pydantic import model_validator as _model_validator

from relay_core.domain.retry import RetryPolicy
from relay_core.domain.transform.convert import ConvertOp

JsonScalar = str | int | float | bool | None

MAX_EACH_DEPTH = 3
MAX_EACH_ITEMS = 1000


class Mapping(BaseModel):
    """Produce one output field from a source path or a constant."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=512)
    source: str | None = Field(default=None, min_length=1, max_length=512)
    const: JsonScalar = None
    default: JsonScalar = None
    required: bool = False
    convert: ConvertOp | None = None
    each: list[Mapping] | None = Field(default=None, min_length=1, max_length=50)

    @property
    def has_const(self) -> bool:
        return "const" in self.model_fields_set

    @property
    def has_default(self) -> bool:
        return "default" in self.model_fields_set

    @_model_validator(mode="after")
    def _check_shape(self) -> Mapping:
        if self.has_const == (self.source is not None):
            raise ValueError("exactly one of 'source' or 'const' must be set")
        if self.has_const and (self.has_default or self.required or self.convert or self.each):
            raise ValueError("'const' cannot be combined with default, required, convert or each")
        if self.required and self.has_default:
            raise ValueError("'required' and 'default' are mutually exclusive")
        if self.each is not None and self.convert is not None:
            raise ValueError("'each' and 'convert' are mutually exclusive")
        if self.each is not None and self.has_default:
            raise ValueError("'each' and 'default' are mutually exclusive")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Presence of const/default is meaningful (null is a legal constant), so
        # unset keys are dropped rather than written as null.
        data: dict[str, Any] = handler(self)
        for key in ("const", "default"):
            if key not in self.model_fields_set:
                data.pop(key, None)
        for key in ("source", "convert", "each"):
            if data.get(key) is None:
                data.pop(key, None)
        if not data.get("required"):
            data.pop("required", None)
        return data


class FilterOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"


_SCALAR_TYPES = (str, int, float, bool, type(None))


class FilterRule(BaseModel):
    """One allowlisted predicate. An event is processed only if every rule matches."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    op: FilterOp
    value: Any = None

    @_model_validator(mode="after")
    def _check_value(self) -> FilterRule:
        has_value = "value" in self.model_fields_set
        if self.op in (FilterOp.EXISTS, FilterOp.NOT_EXISTS):
            if has_value:
                raise ValueError(f"'{self.op.value}' does not take a value")
            return self
        if not has_value:
            raise ValueError(f"'{self.op.value}' requires a value")
        if self.op in (FilterOp.IN, FilterOp.NOT_IN):
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 100:
                raise ValueError(f"'{self.op.value}' requires a list of 1 to 100 scalar values")
            if not all(isinstance(item, _SCALAR_TYPES) for item in self.value):
                raise ValueError(f"'{self.op.value}' values must be scalars")
        elif self.op in (FilterOp.GT, FilterOp.GTE, FilterOp.LT, FilterOp.LTE):
            if isinstance(self.value, bool) or not isinstance(self.value, int | float | str):
                raise ValueError(f"'{self.op.value}' requires a number or string value")
        elif not isinstance(self.value, _SCALAR_TYPES):
            raise ValueError(f"'{self.op.value}' requires a scalar value")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if "value" not in self.model_fields_set:
            data.pop("value", None)
        return data


class DeliveryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=10.0, ge=0.5, le=30)


class IntegrationConfig(BaseModel):
    """Everything an integration version pins. Stored immutably per version."""

    model_config = ConfigDict(extra="forbid")

    event_types: list[str] = Field(default_factory=list, max_length=50)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    filters: list[FilterRule] = Field(default_factory=list, max_length=20)
    passthrough: bool = False
    mappings: list[Mapping] = Field(default_factory=list, max_length=200)
    redact_fields: list[str] = Field(default_factory=list, max_length=50)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    delivery: DeliveryOptions = Field(default_factory=DeliveryOptions)

    def to_storage(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
