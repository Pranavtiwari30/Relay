"""Whitelisted, deterministic value conversions for the transformation DSL.

Each operation documents exactly what it accepts. Anything else raises
``ConversionError`` — conversions never guess and never execute user code.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any


class ConvertOp(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    DATETIME_FROM_EPOCH_SECONDS = "datetime_from_epoch_seconds"
    DATETIME_FROM_EPOCH_MILLIS = "datetime_from_epoch_millis"
    DATE = "date"
    UPPERCASE = "uppercase"
    LOWERCASE = "lowercase"
    TRIM = "trim"


class ConversionError(ValueError):
    pass


_INT_RE = re.compile(r"[+-]?\d{1,19}")
_NUMBER_RE = re.compile(r"[+-]?(\d{1,40}(\.\d{0,40})?|\.\d{1,40})([eE][+-]?\d{1,3})?")
_MAX_INT = 2**63 - 1
_TRUE = frozenset({"true", "1", "yes", "y", "on"})
_FALSE = frozenset({"false", "0", "no", "n", "off"})
# 0001-01-01 .. 9999-12-31 in epoch seconds; keeps datetime arithmetic in range.
_MIN_EPOCH, _MAX_EPOCH = -62_135_596_800, 253_402_300_799


def _type_name(value: Any) -> str:
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
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _to_string(value: Any) -> str:
    """string | integer | number | boolean -> string. Booleans become true/false."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if _is_number(value):
        if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return str(value)
    raise ConversionError(f"cannot convert {_type_name(value)} to string")


def _to_integer(value: Any) -> int:
    """integer, whole-valued number, or a base-10 integer string -> integer."""
    if isinstance(value, bool):
        raise ConversionError("cannot convert boolean to integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ConversionError(f"{value} is not a whole number")
        result = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _INT_RE.fullmatch(text):
            raise ConversionError(f"'{_clip(value)}' is not a base-10 integer")
        result = int(text)
    else:
        raise ConversionError(f"cannot convert {_type_name(value)} to integer")
    if abs(result) > _MAX_INT:
        raise ConversionError("integer is outside the 64-bit signed range")
    return result


def _to_number(value: Any) -> int | float:
    """integer | number, or a decimal string -> number. NaN and infinity are rejected."""
    if isinstance(value, bool):
        raise ConversionError("cannot convert boolean to number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConversionError("number must be finite")
        return value
    if isinstance(value, str):
        text = value.strip()
        if not _NUMBER_RE.fullmatch(text):
            raise ConversionError(f"'{_clip(value)}' is not a decimal number")
        if _INT_RE.fullmatch(text):
            return int(text)
        result = float(text)
        if not math.isfinite(result):
            raise ConversionError("number must be finite")
        return result
    raise ConversionError(f"cannot convert {_type_name(value)} to number")


def _to_boolean(value: Any) -> bool:
    """boolean, 0/1, or true/false/yes/no/on/off/1/0 (case-insensitive) -> boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ConversionError(f"cannot convert {_describe(value)} to boolean")


def _format_utc(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    spec = "microseconds" if moment.microsecond else "seconds"
    return moment.isoformat(timespec=spec).replace("+00:00", "Z")


def _to_datetime(value: Any) -> str:
    """ISO-8601 date-time string -> RFC 3339 UTC string. A value with no offset is read as UTC."""
    if not isinstance(value, str):
        raise ConversionError(f"cannot convert {_type_name(value)} to datetime (expected ISO-8601)")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ConversionError(f"'{_clip(value)}' is not an ISO-8601 date-time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return _format_utc(parsed)
    except (OverflowError, ValueError) as exc:
        raise ConversionError("date-time is out of range") from exc


def _epoch(value: Any, divisor: int) -> str:
    if not _is_number(value) or not math.isfinite(value):
        raise ConversionError(f"cannot convert {_type_name(value)} to datetime (expected epoch)")
    seconds = value / divisor
    if not _MIN_EPOCH <= seconds <= _MAX_EPOCH:
        raise ConversionError("epoch timestamp is out of range")
    return _format_utc(datetime.fromtimestamp(seconds, tz=UTC))


def _to_date(value: Any) -> str:
    """ISO-8601 date or date-time string -> YYYY-MM-DD (date-times are read in UTC)."""
    if not isinstance(value, str):
        raise ConversionError(f"cannot convert {_type_name(value)} to date (expected ISO-8601)")
    text = value.strip()
    try:
        if len(text) <= 10:
            return date.fromisoformat(text).isoformat()
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConversionError(f"'{_clip(value)}' is not an ISO-8601 date") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date().isoformat()


def _string_only(fn: Callable[[str], str], name: str) -> Callable[[Any], str]:
    def apply(value: Any) -> str:
        if not isinstance(value, str):
            raise ConversionError(f"{name} requires a string, got {_type_name(value)}")
        return fn(value)

    return apply


def _clip(value: str, limit: int = 40) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _describe(value: Any) -> str:
    return f"'{_clip(value)}'" if isinstance(value, str) else _type_name(value)


_CONVERTERS: dict[ConvertOp, Callable[[Any], Any]] = {
    ConvertOp.STRING: _to_string,
    ConvertOp.INTEGER: _to_integer,
    ConvertOp.NUMBER: _to_number,
    ConvertOp.BOOLEAN: _to_boolean,
    ConvertOp.DATETIME: _to_datetime,
    ConvertOp.DATETIME_FROM_EPOCH_SECONDS: lambda v: _epoch(v, 1),
    ConvertOp.DATETIME_FROM_EPOCH_MILLIS: lambda v: _epoch(v, 1000),
    ConvertOp.DATE: _to_date,
    ConvertOp.UPPERCASE: _string_only(str.upper, "uppercase"),
    ConvertOp.LOWERCASE: _string_only(str.lower, "lowercase"),
    ConvertOp.TRIM: _string_only(str.strip, "trim"),
}


def convert(value: Any, op: ConvertOp) -> Any:
    return _CONVERTERS[op](value)
