"""Opaque keyset cursors over a stable ``(timestamp, id)`` ordering.

Keyset pagination stays O(page size) at any depth and does not skip or repeat
rows when new events arrive, unlike OFFSET. The cursor is opaque to clients but
not secret: it is validated on the way in and only ever used as a bound inside
a query that is already workspace-scoped.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from relay_core.errors import BadRequestError

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


def encode_cursor(timestamp: datetime, row_id: UUID) -> str:
    raw = json.dumps([timestamp.isoformat(), str(row_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        if len(cursor) > 256:
            raise ValueError("cursor too long")
        padded = cursor + "=" * (-len(cursor) % 4)
        timestamp_raw, id_raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        timestamp = datetime.fromisoformat(timestamp_raw)
        if timestamp.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return timestamp, UUID(id_raw)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise BadRequestError("cursor is not valid", code="invalid_cursor") from exc
