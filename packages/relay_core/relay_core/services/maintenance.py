"""Bounded housekeeping run on each dispatcher tick."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from relay_core.db.models import Event, LocalQueueMessage, OutboxMessage, UserSession
from relay_core.db.session import SessionFactory, session_scope
from relay_core.domain.states import TERMINAL
from relay_core.services import ratelimit

logger = logging.getLogger(__name__)

PURGE_BATCH = 200
PURGED_MARKER = {"_purged": True}


@dataclass
class MaintenanceStats:
    payloads_purged: int = 0
    outbox_rows_deleted: int = 0
    sessions_deleted: int = 0


def purge_expired_payloads(
    session_factory: SessionFactory, *, retention_days: int, now: datetime
) -> int:
    """Remove stored payload bodies from finished events past the retention window.

    The event row, its attempts and its history are kept; only the payload
    bodies go. A purged event can no longer be replayed. ``retention_days=0``
    disables purging.
    """
    if retention_days <= 0:
        return 0
    cutoff = now - timedelta(days=retention_days)
    with session_scope(session_factory) as session:
        ids = list(
            session.execute(
                select(Event.id)
                .where(
                    Event.status.in_([s.value for s in TERMINAL]),
                    Event.updated_at < cutoff,
                    Event.payload_purged_at.is_(None),
                )
                .limit(PURGE_BATCH)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        if ids:
            session.execute(
                update(Event)
                .where(Event.id.in_(ids))
                .values(payload=PURGED_MARKER, transformed_payload=None, payload_purged_at=now)
            )
    return len(ids)


def run_maintenance(
    session_factory: SessionFactory, *, retention_days: int, now: datetime
) -> MaintenanceStats:
    stats = MaintenanceStats()
    stats.payloads_purged = purge_expired_payloads(
        session_factory, retention_days=retention_days, now=now
    )
    week_ago = now - timedelta(days=7)
    with session_scope(session_factory) as session:
        ratelimit.purge_expired(session, now=now)
        # Published and cancelled outbox rows are only useful for short-term debugging.
        stale = list(
            session.execute(
                select(OutboxMessage.id)
                .where(OutboxMessage.status != "pending", OutboxMessage.created_at < week_ago)
                .limit(PURGE_BATCH)
            ).scalars()
        )
        if stale:
            session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(stale)))
        stats.outbox_rows_deleted = len(stale)
        result = session.execute(delete(UserSession).where(UserSession.expires_at < week_ago))
        stats.sessions_deleted = int(getattr(result, "rowcount", 0) or 0)
        session.execute(
            delete(LocalQueueMessage).where(
                LocalQueueMessage.status == "dead", LocalQueueMessage.created_at < week_ago
            )
        )
    if stats.payloads_purged or stats.outbox_rows_deleted:
        logger.info("maintenance completed", extra=stats.__dict__)
    return stats
