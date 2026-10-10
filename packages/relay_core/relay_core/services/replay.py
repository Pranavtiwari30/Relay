"""Operator-initiated replay of failed events.

A replay never rewrites history: it opens a new *delivery cycle* on the same
event. Earlier attempts and transitions stay exactly as they were, and the new
attempts are linked to a ``replay_operations`` row recording who asked and why.

Only ``DEAD_LETTERED`` and ``VALIDATION_FAILED`` events can be replayed. A
``DELIVERED`` event cannot: the destination confirmed it, and re-sending would
be exactly the duplicate side effect Relay exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from relay_core.db.models import Event, Integration, IntegrationVersion, ReplayOperation
from relay_core.domain.states import REPLAYABLE, EventStatus
from relay_core.domain.transform.pipeline import parse_config
from relay_core.errors import ConflictError, NotFoundError, ValidationFailedError
from relay_core.services.audit import record_audit
from relay_core.services.event_state import Actor, transition
from relay_core.services.outbox import schedule_delivery

VersionMode = Literal["original", "current"]


def request_replay(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    event_id: uuid.UUID,
    idempotency_key: str,
    reason: str,
    version_mode: VersionMode,
    now: datetime,
) -> tuple[ReplayOperation, bool]:
    """Start a replay. Returns ``(operation, created)``; the caller commits.

    Idempotent on ``(workspace, idempotency_key)``: a retried request returns
    the original operation instead of replaying twice.
    """
    key = idempotency_key.strip()
    if not 8 <= len(key) <= 128 or not key.isprintable():
        raise ValidationFailedError(
            "Idempotency-Key header must be 8 to 128 printable characters",
            code="invalid_idempotency_key",
        )

    # Lock the event first: serialises concurrent replays of the same event.
    event = session.execute(
        select(Event)
        .where(Event.id == event_id, Event.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if event is None:
        raise NotFoundError("event not found")

    existing = session.execute(
        select(ReplayOperation).where(
            ReplayOperation.workspace_id == workspace_id, ReplayOperation.idempotency_key == key
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.event_id != event_id:
            raise ConflictError(
                "this Idempotency-Key was already used for a different event",
                code="idempotency_key_reused",
            )
        return existing, False

    status = EventStatus(event.status)
    if status not in REPLAYABLE:
        raise ConflictError(
            f"an event in status {status.value} cannot be replayed; only "
            + " and ".join(sorted(s.value for s in REPLAYABLE))
            + " events can",
            code="not_replayable",
        )
    if event.payload_purged_at is not None:
        raise ConflictError(
            "the stored payload was removed by the retention policy, so this event cannot be replayed",
            code="payload_purged",
        )
    integration = session.get_one(Integration, event.integration_id)
    if integration.status not in ("active", "paused"):
        raise ConflictError(
            f"the integration is {integration.status}; activate it before replaying events",
            code="integration_not_active",
        )

    version_id = event.integration_version_id
    if version_mode == "current":
        if integration.current_version_id is None:
            raise ConflictError(
                "the integration has no current version", code="integration_not_active"
            )
        version_id = integration.current_version_id

    version = session.get_one(IntegrationVersion, version_id)
    config, _ = parse_config(version.config)

    transition(
        session, event, EventStatus.REPLAY_REQUESTED, reason="replay_requested", actor=actor, now=now,
        details={"version_mode": version_mode, "from_status": status.value},
    )  # fmt: skip
    # A new cycle with a fresh attempt budget. Old attempts keep their cycle
    # number, so history is preserved and never renumbered.
    event.delivery_cycle += 1
    event.attempt_count = 0
    event.integration_version_id = version_id
    event.max_attempts = config.retry.max_attempts if config is not None else event.max_attempts
    event.transformed_payload = None
    event.dead_lettered_at = None
    operation = ReplayOperation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        event_id=event.id,
        requested_by=actor.user_id,
        reason=reason.strip()[:1000],
        idempotency_key=key,
        version_mode=version_mode,
        integration_version_id=version_id,
        from_status=status.value,
        cycle=event.delivery_cycle,
        created_at=now,
    )
    session.add(operation)
    session.flush()

    if integration.status == "paused":
        transition(
            session, event, EventStatus.HELD, reason="integration_paused", actor=actor, now=now
        )
    else:
        transition(
            session,
            event,
            EventStatus.PENDING_PUBLISH,
            reason="replay_enqueued",
            actor=actor,
            now=now,
        )
        schedule_delivery(session, event, attempt_number=1, available_at=now)

    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="event.replay",
        entity_type="event",
        entity_id=event.id,
        details={
            "replay_operation_id": str(operation.id),
            "from_status": status.value,
            "cycle": operation.cycle,
            "version_mode": version_mode,
            "integration_version_id": str(version_id),
            "reason": operation.reason,
        },
    )
    return operation, True
