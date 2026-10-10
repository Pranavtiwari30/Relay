"""Bounded JSON path parsing and traversal for the transformation DSL.

Grammar: ``segment ( "." segment | "[" index "]" )*`` — for example
``customer.address.city`` or ``items[0].sku``. There are no wildcards, filters,
recursion or expressions, and both depth and index are capped, so evaluating a
path is O(depth) with a small constant bound.
"""

from __future__ import annotations

from typing import Any, Final

MAX_PATH_SEGMENTS: Final = 12
MAX_KEY_LENGTH: Final = 128
MAX_ARRAY_INDEX: Final = 9_999


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()

Segment = str | int


class PathSyntaxError(ValueError):
    pass


def parse_path(path: str, *, allow_indexes: bool = True) -> tuple[Segment, ...]:
    if not isinstance(path, str) or not path:
        raise PathSyntaxError("path must be a non-empty string")
    segments: list[Segment] = []
    pos, length = 0, len(path)
    expect_key = True
    while pos < length:
        char = path[pos]
        if char == "[":
            if expect_key and not segments:
                raise PathSyntaxError("path cannot start with an index")
            end = path.find("]", pos)
            if end == -1:
                raise PathSyntaxError("unclosed '[' in path")
            raw = path[pos + 1 : end]
            if not raw.isascii() or not raw.isdigit():
                raise PathSyntaxError(f"array index must be a non-negative integer, got '{raw}'")
            index = int(raw)
            if index > MAX_ARRAY_INDEX:
                raise PathSyntaxError(f"array index exceeds the limit of {MAX_ARRAY_INDEX}")
            if not allow_indexes:
                raise PathSyntaxError("array indexes are not allowed in a target path")
            segments.append(index)
            pos = end + 1
            expect_key = False
            continue
        if char == ".":
            if expect_key:
                raise PathSyntaxError("empty path segment")
            pos += 1
            expect_key = True
            continue
        if not expect_key:
            raise PathSyntaxError("expected '.' or '[' after an index")
        end = pos
        while end < length and path[end] not in ".[":
            if path[end] == "]" or ord(path[end]) < 0x20:
                raise PathSyntaxError("invalid character in path segment")
            end += 1
        key = path[pos:end]
        if len(key) > MAX_KEY_LENGTH:
            raise PathSyntaxError(f"path segment exceeds {MAX_KEY_LENGTH} characters")
        segments.append(key)
        pos = end
        expect_key = False
    if expect_key:
        raise PathSyntaxError("path cannot end with '.'")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise PathSyntaxError(f"path has more than {MAX_PATH_SEGMENTS} segments")
    return tuple(segments)


def get_path(document: Any, segments: tuple[Segment, ...]) -> Any:
    """Return the value at ``segments`` or ``MISSING``. Never raises on shape mismatch."""
    current = document
    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return MISSING
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return MISSING
            current = current[segment]
    return current


class PathConflictError(ValueError):
    pass


def set_path(target: dict[str, Any], segments: tuple[Segment, ...], value: Any) -> None:
    """Write ``value`` into nested objects, creating intermediate objects.

    Raises ``PathConflictError`` rather than silently overwriting when two
    mappings disagree about the shape of the output.
    """
    current = target
    for segment in segments[:-1]:
        if not isinstance(segment, str):
            raise PathConflictError("target paths may only contain object keys")
        existing = current.get(segment, MISSING)
        if existing is MISSING:
            created: dict[str, Any] = {}
            current[segment] = created
            current = created
        elif isinstance(existing, dict):
            current = existing
        else:
            raise PathConflictError(f"'{segment}' is already set to a non-object value")
    last = segments[-1]
    if not isinstance(last, str):
        raise PathConflictError("target paths may only contain object keys")
    if last in current:
        raise PathConflictError(f"'{last}' is written by more than one mapping")
    current[last] = value


def format_path(segments: tuple[Segment, ...]) -> str:
    out = ""
    for segment in segments:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            out += f".{segment}" if out else segment
    return out
