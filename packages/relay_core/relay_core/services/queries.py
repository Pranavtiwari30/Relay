"""Read models: event explorer, audit log and the dashboard.

Every number the dashboard shows is computed here from stored rows. Nothing is
estimated, cached across requests, or hardcoded.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.orm import Session

from relay_core.db.models import (
    AuditLog,
    DeliveryAttempt,
    Event,
    EventTransition,
    Integration,
    Investigation,
    ReplayOperation,
    User,
)
from relay_core.domain.pagination import Page, clamp_limit, decode_cursor, encode_cursor
from relay_core.domain.states import FAILED, IN_FLIGHT, EventStatus
from relay_core.errors import NotFoundError
from relay_core.services.outbox import oldest_pending_age_seconds


@dataclass
class EventFilters:
    statuses: list[str] = field(default_factory=list)
    integration_id: uuid.UUID | None = None
    received_after: datetime | None = None
    received_before: datetime | None = None
    search: str | None = None


def list_events(
    session: Session,
    workspace_id: uuid.UUID,
    filters: EventFilters,
    *,
    cursor: str | None,
    limit: int | None,
) -> Page[Event]:
    size = clamp_limit(limit)
    query = select(Event).where(Event.workspace_id == workspace_id)
    if filters.statuses:
        query = query.where(Event.status.in_(filters.statuses))
    if filters.integration_id is not None:
        query = query.where(Event.integration_id == filters.integration_id)
    if filters.received_after is not None:
        query = query.where(Event.received_at >= filters.received_after)
    if filters.received_before is not None:
        query = query.where(Event.received_at < filters.received_before)
    if filters.search:
        term = filters.search.strip()[:255]
        clauses = [Event.source_event_id == term, Event.correlation_id == term]
        with contextlib.suppress(ValueError):
            clauses.append(Event.id == uuid.UUID(term))
        query = query.where(or_(*clauses))
    if cursor:
        timestamp, row_id = decode_cursor(cursor)
        # Row-value comparison matches the (received_at DESC, id DESC) index order.
        query = query.where(tuple_(Event.received_at, Event.id) < tuple_(timestamp, row_id))
    rows = list(
        session.execute(
            query.order_by(Event.received_at.desc(), Event.id.desc()).limit(size + 1)
        ).scalars()
    )
    next_cursor = None
    if len(rows) > size:
        rows = rows[:size]
        next_cursor = encode_cursor(rows[-1].received_at, rows[-1].id)
    return Page(rows, next_cursor)


def get_event(session: Session, workspace_id: uuid.UUID, event_id: uuid.UUID) -> Event:
    event = session.execute(
        select(Event).where(Event.id == event_id, Event.workspace_id == workspace_id)
    ).scalar_one_or_none()
    if event is None:
        raise NotFoundError("event not found")
    return event


def list_attempts(
    session: Session, workspace_id: uuid.UUID, event_id: uuid.UUID
) -> list[DeliveryAttempt]:
    return list(
        session.execute(
            select(DeliveryAttempt)
            .where(
                DeliveryAttempt.event_id == event_id, DeliveryAttempt.workspace_id == workspace_id
            )
            .order_by(DeliveryAttempt.cycle, DeliveryAttempt.attempt_number)
        ).scalars()
    )


def list_transitions(
    session: Session, workspace_id: uuid.UUID, event_id: uuid.UUID
) -> list[EventTransition]:
    return list(
        session.execute(
            select(EventTransition)
            .where(
                EventTransition.event_id == event_id, EventTransition.workspace_id == workspace_id
            )
            .order_by(EventTransition.id)
        ).scalars()
    )


def list_replays(
    session: Session, workspace_id: uuid.UUID, event_id: uuid.UUID
) -> list[ReplayOperation]:
    return list(
        session.execute(
            select(ReplayOperation)
            .where(
                ReplayOperation.event_id == event_id, ReplayOperation.workspace_id == workspace_id
            )
            .order_by(ReplayOperation.cycle)
        ).scalars()
    )


def list_audit(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int | None,
    action_prefix: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> Page[tuple[AuditLog, str | None]]:
    size = clamp_limit(limit)
    query = (
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .where(AuditLog.workspace_id == workspace_id)
    )
    if action_prefix:
        query = query.where(AuditLog.action.startswith(action_prefix[:80], autoescape=True))
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if cursor:
        timestamp, row_id = decode_cursor(cursor)
        query = query.where(tuple_(AuditLog.created_at, AuditLog.id) < tuple_(timestamp, row_id))
    rows = session.execute(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(size + 1)
    ).all()
    items: list[tuple[AuditLog, str | None]] = [(row[0], row[1]) for row in rows]
    next_cursor = None
    if len(items) > size:
        items = items[:size]
        next_cursor = encode_cursor(items[-1][0].created_at, items[-1][0].id)
    return Page(items, next_cursor)


WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

_FAILED = [s.value for s in FAILED]
_IN_FLIGHT = [s.value for s in IN_FLIGHT]


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def dashboard_summary(
    session: Session, workspace_id: uuid.UUID, *, window: str, now: datetime
) -> dict[str, Any]:
    since = now - WINDOWS[window]
    in_window = (Event.workspace_id == workspace_id) & (Event.received_at >= since)

    by_status = {
        status: int(count)
        for status, count in session.execute(
            select(Event.status, func.count()).where(in_window).group_by(Event.status)
        ).all()
    }
    delivered = by_status.get(EventStatus.DELIVERED.value, 0)
    failed = sum(by_status.get(s, 0) for s in _FAILED)
    in_flight = sum(by_status.get(s, 0) for s in _IN_FLIGHT)
    received = sum(by_status.values())

    attempts = session.execute(
        select(
            func.count(),
            func.count().filter(DeliveryAttempt.status == "failed"),
            func.percentile_cont(0.5).within_group(DeliveryAttempt.duration_ms),
            func.percentile_cont(0.95).within_group(DeliveryAttempt.duration_ms),
            func.count().filter(DeliveryAttempt.attempt_number > 1),
        ).where(
            DeliveryAttempt.workspace_id == workspace_id,
            DeliveryAttempt.started_at >= since,
            DeliveryAttempt.status != "in_progress",
        )
    ).one()

    # "Right now" figures are deliberately not windowed: a stuck event from last
    # week is still stuck.
    live = session.execute(
        select(
            func.count().filter(Event.status == EventStatus.RETRY_SCHEDULED.value),
            func.count().filter(Event.status == EventStatus.HELD.value),
            func.min(Event.received_at).filter(Event.status.in_(_IN_FLIGHT)),
        ).where(
            Event.workspace_id == workspace_id,
            Event.status.in_([*_IN_FLIGHT, EventStatus.HELD.value]),
        )
    ).one()
    oldest_in_flight = live[2]

    recent_failures = list(
        session.execute(
            select(Event)
            .where(Event.workspace_id == workspace_id, Event.status.in_(_FAILED))
            .order_by(Event.updated_at.desc())
            .limit(8)
        ).scalars()
    )

    delivered_case = case((Event.status == EventStatus.DELIVERED.value, 1), else_=0)
    failed_case = case((Event.status.in_(_FAILED), 1), else_=0)
    flight_case = case((Event.status.in_(_IN_FLIGHT), 1), else_=0)
    health_rows = session.execute(
        select(
            Integration.id,
            Integration.name,
            Integration.status,
            func.count(Event.id),
            func.coalesce(func.sum(delivered_case), 0),
            func.coalesce(func.sum(failed_case), 0),
            func.coalesce(func.sum(flight_case), 0),
            func.max(Event.received_at),
        )
        .outerjoin(Event, (Event.integration_id == Integration.id) & (Event.received_at >= since))
        .where(Integration.workspace_id == workspace_id)
        .group_by(Integration.id)
        .order_by(Integration.name)
    ).all()

    unit = "hour" if WINDOWS[window] <= timedelta(hours=48) else "day"
    bucket = func.date_trunc(unit, Event.received_at)
    series = session.execute(
        select(bucket, func.count(), func.sum(delivered_case), func.sum(failed_case))
        .where(in_window)
        .group_by(bucket)
        .order_by(bucket)
    ).all()

    ai = session.execute(
        select(
            func.count(),
            func.count().filter(Investigation.mode == "ai"),
            func.count().filter(Investigation.fallback_reason.is_not(None)),
            func.count().filter(Investigation.status == "failed"),
            func.avg(Investigation.duration_ms),
        ).where(Investigation.workspace_id == workspace_id, Investigation.created_at >= since)
    ).one()

    return {
        "window": window,
        "since": since,
        "generated_at": now,
        "events": {
            "received": received,
            "delivered": delivered,
            "failed": failed,
            "in_flight": in_flight,
            "filtered": by_status.get(EventStatus.FILTERED.value, 0),
            "held": by_status.get(EventStatus.HELD.value, 0),
            "dead_lettered": by_status.get(EventStatus.DEAD_LETTERED.value, 0),
            "validation_failed": by_status.get(EventStatus.VALIDATION_FAILED.value, 0),
            "by_status": by_status,
            # Of events that reached an outcome, the share that were delivered.
            "success_rate": _ratio(delivered, delivered + failed),
        },
        "attempts": {
            "total": int(attempts[0]),
            "failed": int(attempts[1]),
            "retries": int(attempts[4]),
            "duration_ms_p50": round(float(attempts[2]), 1) if attempts[2] is not None else None,
            "duration_ms_p95": round(float(attempts[3]), 1) if attempts[3] is not None else None,
        },
        "live": {
            "retry_scheduled": int(live[0]),
            "held": int(live[1]),
            "oldest_in_flight_age_seconds": (
                round((now - oldest_in_flight).total_seconds(), 1) if oldest_in_flight else None
            ),
            "oldest_pending_outbox_age_seconds": oldest_pending_age_seconds(
                session, now=now, workspace_id=workspace_id
            ),
        },
        "recent_failures": recent_failures,
        "integrations": [
            {
                "id": row[0],
                "name": row[1],
                "status": row[2],
                "received": int(row[3]),
                "delivered": int(row[4]),
                "failed": int(row[5]),
                "in_flight": int(row[6]),
                "last_event_at": row[7],
                "success_rate": _ratio(int(row[4]), int(row[4]) + int(row[5])),
            }
            for row in health_rows
        ],
        "series": {
            "bucket": unit,
            "points": [
                {
                    "at": row[0],
                    "received": int(row[1]),
                    "delivered": int(row[2] or 0),
                    "failed": int(row[3] or 0),
                }
                for row in series
            ],
        },
        "investigations": {
            "total": int(ai[0]),
            "ai": int(ai[1]),
            "fell_back": int(ai[2]),
            "failed": int(ai[3]),
            "avg_duration_ms": round(float(ai[4]), 1) if ai[4] is not None else None,
        },
    }
