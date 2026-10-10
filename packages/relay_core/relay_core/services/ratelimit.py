"""Fixed-window rate limiting backed by PostgreSQL.

Counters live in the database, so the limit holds across every API instance —
a process-local token bucket would silently multiply the limit by the instance
count. The trade-off is one small write per counted request; see
``docs/architecture/algorithms.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from relay_core.db.models import RateLimitCounter
from relay_core.db.session import SessionFactory, session_scope


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    count: int
    retry_after_seconds: int


def window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=UTC)


def hit(
    session_factory: SessionFactory,
    scope_key: str,
    *,
    limit: int,
    window_seconds: int,
    now: datetime,
) -> RateDecision:
    """Count one request against ``scope_key`` and say whether it is within the limit.

    A single atomic upsert: concurrent requests on any number of instances are
    serialised by the row lock, so the count cannot be under-read.
    """
    start = window_start(now, window_seconds)
    statement = (
        pg_insert(RateLimitCounter)
        .values(scope_key=scope_key[:200], window_start=start, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimitCounter.scope_key, RateLimitCounter.window_start],
            set_={"count": RateLimitCounter.count + 1},
        )
        .returning(RateLimitCounter.count)
    )
    with session_scope(session_factory) as session:
        count = int(session.execute(statement).scalar_one())
    retry_after = max(1, int((start + timedelta(seconds=window_seconds) - now).total_seconds()))
    return RateDecision(count <= limit, limit, count, retry_after)


def purge_expired(session: Session, *, now: datetime, keep_seconds: int = 3600) -> int:
    result = session.execute(
        delete(RateLimitCounter).where(
            RateLimitCounter.window_start < now - timedelta(seconds=keep_seconds)
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)
