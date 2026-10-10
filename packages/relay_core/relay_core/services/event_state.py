"""The only place an event's status is changed.

Every status change goes through ``transition`` so that it is (a) checked
against the state machine, (b) written to the append-only history in the same
transaction, and (c) keeps derived timestamps consistent. A unit test fails the
build if any other module assigns ``Event.status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from relay_core.db.models import Event, EventTransition
from relay_core.domain.states import EventStatus, can_transition
from relay_core.errors import InvalidTransitionError


@dataclass(frozen=True)
class Actor:
    """Who caused a change: a signed-in user or a named system component."""

    type: str
    user_id: UUID | None = None

    @classmethod
    def user(cls, user_id: UUID) -> Actor:
        return cls("user", user_id)


INGESTION = Actor("ingestion")
DISPATCHER = Actor("dispatcher")
WORKER = Actor("worker")
SYSTEM = Actor("system")


def record_received(session: Session, event: Event, *, now: datetime) -> None:
    """History entry for the initial persist (there is no previous status)."""
    session.add(
        EventTransition(
            event_id=event.id,
            workspace_id=event.workspace_id,
            from_status=None,
            to_status=EventStatus.RECEIVED.value,
            reason="received",
            actor_type=INGESTION.type,
            created_at=now,
        )
    )


def transition(
    session: Session,
    event: Event,
    target: EventStatus,
    *,
    reason: str,
    actor: Actor,
    now: datetime,
    details: dict[str, Any] | None = None,
) -> None:
    """Move ``event`` to ``target`` or raise ``InvalidTransitionError``.

    The caller must hold a row lock on the event (``SELECT ... FOR UPDATE``) or
    have just inserted it; the change commits with the caller's transaction.
    ``details`` must contain safe metadata only — never payload values.
    """
    current = EventStatus(event.status)
    if not can_transition(current, target):
        raise InvalidTransitionError(
            f"event cannot move from {current.value} to {target.value}",
            details=[{"from": current.value, "to": target.value}],
        )
    event.status = target.value
    event.updated_at = now
    if target is not EventStatus.PROCESSING:
        event.processing_lease_expires_at = None
    if target is not EventStatus.RETRY_SCHEDULED:
        event.next_attempt_at = None
    if target is EventStatus.DELIVERED:
        event.delivered_at = now
        event.failure_stage = None
        event.failure_category = None
        event.last_error = None
        event.failure_details = None
    elif target is EventStatus.DEAD_LETTERED:
        event.dead_lettered_at = now
    session.add(
        EventTransition(
            event_id=event.id,
            workspace_id=event.workspace_id,
            from_status=current.value,
            to_status=target.value,
            reason=reason,
            actor_type=actor.type,
            actor_user_id=actor.user_id,
            details=details,
            created_at=now,
        )
    )
