"""Transactional outbox: durable hand-off from PostgreSQL to the message broker.

Writers insert an outbox row in the *same transaction* as the event change, so
"event accepted" and "a message will be published" commit or roll back together.
The dispatcher then publishes rows with at-least-once semantics:

    claim (lease)  ->  publish  ->  mark published

* Claiming uses ``FOR UPDATE SKIP LOCKED`` so concurrent dispatchers never block
  each other or claim the same row.
* A row is marked ``published`` only after the broker confirms.
* If the dispatcher dies after publishing but before marking, the lease expires
  and the row is published again. The worker discards the duplicate by comparing
  the message's ``(cycle, attempt)`` with the event's state.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from relay_core.db.models import Event, OutboxMessage
from relay_core.db.session import SessionFactory, session_scope, utcnow
from relay_core.domain.states import EventStatus
from relay_core.logging import bind_log_context
from relay_core.messaging.base import DeliveryMessage, Publisher
from relay_core.services.event_state import DISPATCHER, transition

logger = logging.getLogger(__name__)

MAX_PUBLISH_BACKOFF_SECONDS = 60.0


def schedule_delivery(
    session: Session, event: Event, *, attempt_number: int, available_at: datetime
) -> uuid.UUID:
    """Insert (or re-arm) the outbox row for one delivery attempt.

    Idempotent on ``(event, cycle, attempt)``: scheduling the same attempt twice
    re-arms the existing row instead of creating a second message.
    """
    message = DeliveryMessage(
        event_id=event.id,
        workspace_id=event.workspace_id,
        cycle=event.delivery_cycle,
        attempt=attempt_number,
        correlation_id=event.correlation_id,
    )
    statement = (
        pg_insert(OutboxMessage)
        .values(
            id=uuid.uuid4(),
            event_id=event.id,
            workspace_id=event.workspace_id,
            cycle=event.delivery_cycle,
            attempt_number=attempt_number,
            payload=message.to_data(),
            status="pending",
            available_at=available_at,
        )
        .on_conflict_do_update(
            constraint="uq_outbox_event_attempt",
            set_={
                "status": "pending",
                "available_at": available_at,
                "claim_token": None,
                "lease_expires_at": None,
                "published_at": None,
                "broker_message_id": None,
            },
        )
        .returning(OutboxMessage.id)
    )
    return uuid.UUID(str(session.execute(statement).scalar_one()))


@dataclass
class DispatchStats:
    claimed: int = 0
    published: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _Claim:
    outbox_id: uuid.UUID
    event_id: uuid.UUID
    cycle: int
    attempt_number: int
    payload: dict[str, Any]
    publish_attempts: int
    token: uuid.UUID


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        publisher: Publisher,
        *,
        lease_seconds: int = 30,
        batch_size: int = 50,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._lease = timedelta(seconds=lease_seconds)
        self._batch_size = batch_size
        self._clock = clock

    def dispatch_due(self, limit: int | None = None) -> DispatchStats:
        """Publish pending messages whose ``available_at`` has passed."""
        return self._publish(self._claim(limit or self._batch_size, None))

    def dispatch_ids(self, outbox_ids: Sequence[uuid.UUID]) -> DispatchStats:
        """Best-effort immediate publish of specific rows (the ingestion fast path)."""
        if not outbox_ids:
            return DispatchStats()
        return self._publish(self._claim(len(outbox_ids), outbox_ids))

    def _claim(self, limit: int, outbox_ids: Sequence[uuid.UUID] | None) -> list[_Claim]:
        now = self._clock()
        token = uuid.uuid4()
        query = select(OutboxMessage).where(
            OutboxMessage.status == "pending",
            OutboxMessage.available_at <= now,
            or_(OutboxMessage.lease_expires_at.is_(None), OutboxMessage.lease_expires_at < now),
        )
        if outbox_ids is not None:
            query = query.where(OutboxMessage.id.in_(outbox_ids))
        query = (
            query.order_by(OutboxMessage.available_at, OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        with session_scope(self._session_factory) as session:
            rows = list(session.execute(query).scalars())
            for row in rows:
                row.claim_token = token
                row.lease_expires_at = now + self._lease
                row.publish_attempts += 1
            return [
                _Claim(
                    r.id,
                    r.event_id,
                    r.cycle,
                    r.attempt_number,
                    dict(r.payload),
                    r.publish_attempts,
                    token,
                )
                for r in rows
            ]

    def _publish(self, claims: list[_Claim]) -> DispatchStats:
        stats = DispatchStats(claimed=len(claims))
        for claim in claims:
            with bind_log_context(
                event_id=str(claim.event_id),
                correlation_id=claim.payload.get("correlation_id"),
                outbox_id=str(claim.outbox_id),
            ):
                attributes = {
                    "event_id": str(claim.event_id),
                    "correlation_id": str(claim.payload.get("correlation_id", "")),
                }
                try:
                    broker_id = self._publisher.publish(claim.payload, attributes)
                except Exception as exc:
                    stats.failed += 1
                    logger.warning("outbox publish failed", extra={"error": type(exc).__name__})
                    self._release(claim, f"{type(exc).__name__}: {exc}"[:500])
                    continue
                try:
                    self._mark_published(claim, broker_id)
                    stats.published += 1
                    logger.info("outbox message published", extra={"broker_message_id": broker_id})
                except Exception:
                    # The broker has the message but we could not record it. The
                    # lease will lapse and the row is published again; the worker
                    # ignores the duplicate.
                    stats.failed += 1
                    logger.exception("published but could not mark outbox row; will republish")
        return stats

    def _release(self, claim: _Claim, error: str) -> None:
        backoff = min(MAX_PUBLISH_BACKOFF_SECONDS, 2.0 ** min(claim.publish_attempts, 10))
        try:
            with session_scope(self._session_factory) as session:
                session.execute(
                    update(OutboxMessage)
                    .where(
                        OutboxMessage.id == claim.outbox_id,
                        OutboxMessage.claim_token == claim.token,
                    )
                    .values(
                        claim_token=None,
                        lease_expires_at=None,
                        available_at=self._clock() + timedelta(seconds=backoff),
                        last_error=error,
                    )
                )
        except Exception:
            logger.exception("could not release outbox claim; the lease will expire on its own")

    def _mark_published(self, claim: _Claim, broker_id: str) -> None:
        now = self._clock()
        with session_scope(self._session_factory) as session:
            # Lock order is always event -> outbox, matching every other writer.
            event = session.execute(
                select(Event).where(Event.id == claim.event_id).with_for_update()
            ).scalar_one_or_none()
            row = session.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.id == claim.outbox_id, OutboxMessage.claim_token == claim.token
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return  # lease lapsed and another dispatcher took over
            row.status = "published"
            row.published_at = now
            row.broker_message_id = broker_id[:255]
            row.claim_token = None
            row.lease_expires_at = None
            row.last_error = None
            # The worker may already have picked the message up; only advance the
            # event if it is still waiting for exactly this attempt.
            if (
                event is not None
                and event.status in (EventStatus.PENDING_PUBLISH, EventStatus.RETRY_SCHEDULED)
                and event.delivery_cycle == claim.cycle
                and event.attempt_count + 1 == claim.attempt_number
            ):
                transition(
                    session,
                    event,
                    EventStatus.QUEUED,
                    reason="published",
                    actor=DISPATCHER,
                    now=now,
                    details={"attempt": claim.attempt_number, "cycle": claim.cycle},
                )


def cancel_pending(session: Session, event_id: uuid.UUID) -> None:
    """Cancel unpublished messages for an event (used when it is held or dead-lettered)."""
    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.event_id == event_id, OutboxMessage.status == "pending")
        .values(status="cancelled", claim_token=None, lease_expires_at=None)
    )


def oldest_pending_age_seconds(
    session: Session, *, now: datetime, workspace_id: uuid.UUID | None = None
) -> float | None:
    """Age of the oldest message that is due but not yet published (a health signal)."""
    query = select(func.min(OutboxMessage.available_at)).where(
        OutboxMessage.status == "pending", OutboxMessage.available_at <= now
    )
    if workspace_id is not None:
        query = query.where(OutboxMessage.workspace_id == workspace_id)
    oldest = session.execute(query).scalar_one_or_none()
    return None if oldest is None else max(0.0, (now - oldest).total_seconds())
