"""Event lifecycle finite-state machine.

This table is the single definition of which event status changes are legal.
``relay_core.services.event_state.transition`` is the only code that assigns
``Event.status``; it consults this table and records every change. See
``docs/architecture/reliability.md`` for owners and transactional boundaries.
"""

from __future__ import annotations

from enum import StrEnum


class EventStatus(StrEnum):
    RECEIVED = "RECEIVED"
    HELD = "HELD"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    FILTERED = "FILTERED"
    PENDING_PUBLISH = "PENDING_PUBLISH"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DELIVERED = "DELIVERED"
    DEAD_LETTERED = "DEAD_LETTERED"
    REPLAY_REQUESTED = "REPLAY_REQUESTED"


S = EventStatus

TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    # Ingestion persists the event, then routes it in the same transaction.
    S.RECEIVED: frozenset({S.VALIDATION_FAILED, S.PENDING_PUBLISH, S.HELD}),
    # Integration paused: nothing is delivered until it is resumed or disabled.
    S.HELD: frozenset({S.PENDING_PUBLISH, S.DEAD_LETTERED}),
    # An outbox row exists; the broker has not confirmed the message yet. A
    # worker may legitimately see the message before the dispatcher records
    # QUEUED, hence the direct edge to PROCESSING.
    S.PENDING_PUBLISH: frozenset({S.QUEUED, S.PROCESSING, S.HELD, S.DEAD_LETTERED}),
    S.QUEUED: frozenset({S.PROCESSING, S.HELD, S.DEAD_LETTERED}),
    S.PROCESSING: frozenset(
        {S.DELIVERED, S.RETRY_SCHEDULED, S.DEAD_LETTERED, S.VALIDATION_FAILED, S.FILTERED}
    ),
    S.RETRY_SCHEDULED: frozenset({S.QUEUED, S.PROCESSING, S.HELD, S.DEAD_LETTERED}),
    # Operator-initiated recovery only.
    S.VALIDATION_FAILED: frozenset({S.REPLAY_REQUESTED}),
    S.DEAD_LETTERED: frozenset({S.REPLAY_REQUESTED}),
    S.REPLAY_REQUESTED: frozenset({S.PENDING_PUBLISH, S.HELD}),
    # Terminal. A delivered event is never re-sent: the destination confirmed it.
    S.DELIVERED: frozenset(),
    S.FILTERED: frozenset(),
}

TERMINAL: frozenset[EventStatus] = frozenset(
    {S.DELIVERED, S.FILTERED, S.VALIDATION_FAILED, S.DEAD_LETTERED}
)
REPLAYABLE: frozenset[EventStatus] = frozenset({S.VALIDATION_FAILED, S.DEAD_LETTERED})
# Statuses from which a worker may claim the event for a delivery attempt.
CLAIMABLE: frozenset[EventStatus] = frozenset({S.PENDING_PUBLISH, S.QUEUED, S.RETRY_SCHEDULED})
# Statuses that still owe the operator an outcome.
IN_FLIGHT: frozenset[EventStatus] = frozenset(
    {S.RECEIVED, S.PENDING_PUBLISH, S.QUEUED, S.PROCESSING, S.RETRY_SCHEDULED, S.REPLAY_REQUESTED}
)
FAILED: frozenset[EventStatus] = frozenset({S.VALIDATION_FAILED, S.DEAD_LETTERED})


def can_transition(current: EventStatus, target: EventStatus) -> bool:
    return target in TRANSITIONS[current]
