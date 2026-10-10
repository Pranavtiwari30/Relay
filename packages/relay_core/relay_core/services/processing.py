"""Worker-side delivery processing.

One broker message means "run delivery attempt N of cycle C for event E".
Handling is split into three short steps so that no database lock is held
across the network call to the destination:

    1. claim    (txn)  lock event, discard duplicates, run the pipeline,
                       record an in-progress attempt and a lease
    2. deliver  (none) call the destination adapter
    3. complete (txn)  lock event, record the outcome, schedule a retry or finish

Guarantee: at-least-once delivery. The destination may see the same event more
than once (for example when a response is lost), which is why every request
carries a stable idempotency key. See ``docs/architecture/reliability.md``.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relay_core.adapters.base import DeliveryRequest, DeliveryResult, DestinationAdapter
from relay_core.adapters.registry import AdapterRegistry
from relay_core.db.models import (
    Connection,
    DeliveryAttempt,
    Event,
    Integration,
    IntegrationVersion,
    ReplayOperation,
)
from relay_core.db.session import SessionFactory, session_scope, utcnow
from relay_core.domain.retry import Disposition, FailureCategory, RetryPolicy, decide_retry
from relay_core.domain.states import CLAIMABLE, EventStatus
from relay_core.domain.transform.models import IntegrationConfig
from relay_core.domain.transform.pipeline import parse_config, run_pipeline
from relay_core.logging import bind_log_context
from relay_core.messaging.base import DeliveryMessage
from relay_core.services.event_state import WORKER, Actor, transition
from relay_core.services.outbox import cancel_pending, schedule_delivery
from relay_core.services.secrets import SecretResolver

logger = logging.getLogger(__name__)


class Ack(StrEnum):
    """What to tell the broker."""

    ACK = "ack"  # done, or can never succeed: do not redeliver
    NACK = "nack"  # transient: redeliver later


@dataclass(frozen=True)
class HandleResult:
    ack: Ack
    detail: str


@dataclass(frozen=True)
class _Claimed:
    attempt_id: uuid.UUID
    adapter: DestinationAdapter
    destination_config: dict[str, Any]
    secret: str | None
    request: DeliveryRequest


INTERRUPTED_MESSAGE = (
    "the worker did not record an outcome before its lease expired; "
    "the destination may or may not have received this attempt"
)


class DeliveryProcessor:
    def __init__(
        self,
        session_factory: SessionFactory,
        registry: AdapterRegistry,
        secrets: SecretResolver,
        *,
        lease_margin_seconds: int = 30,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._secrets = secrets
        self._lease_margin = timedelta(seconds=lease_margin_seconds)
        self._clock = clock
        # Jitter only spreads retries out; it has no security role.
        self._rng = rng or random.Random()  # noqa: S311

    # -- entry points -------------------------------------------------------

    def handle(self, message: DeliveryMessage) -> HandleResult:
        with bind_log_context(
            event_id=str(message.event_id),
            workspace_id=str(message.workspace_id),
            correlation_id=message.correlation_id,
            attempt=message.attempt,
            cycle=message.cycle,
        ):
            claimed = self._claim(message)
            if isinstance(claimed, HandleResult):
                logger.info(
                    "delivery message handled without an attempt", extra={"detail": claimed.detail}
                )
                return claimed
            try:
                result = claimed.adapter.deliver(
                    claimed.destination_config, claimed.secret, claimed.request
                )
            except Exception as exc:
                # An adapter bug must not strand the event in PROCESSING.
                logger.exception("destination adapter raised unexpectedly")
                result = DeliveryResult(
                    disposition=Disposition.RETRYABLE,
                    duration_ms=0,
                    failure_category=FailureCategory.TRANSPORT,
                    error_message=f"unexpected delivery error ({type(exc).__name__})",
                )
            outcome = self._complete(message, claimed.attempt_id, result)
            logger.info(
                "delivery attempt finished",
                extra={
                    "disposition": result.disposition.value,
                    "failure_category": result.failure_category.value
                    if result.failure_category
                    else None,
                    "http_status": result.http_status,
                    "duration_ms": result.duration_ms,
                    "detail": outcome.detail,
                },
            )
            return outcome

    def reap_expired_leases(self, limit: int = 50) -> int:
        """Recover events whose worker vanished mid-attempt (crash, kill, timeout)."""
        now = self._clock()
        with session_scope(self._session_factory) as session:
            events = list(
                session.execute(
                    select(Event)
                    .where(
                        Event.status == EventStatus.PROCESSING.value,
                        Event.processing_lease_expires_at < now,
                    )
                    .order_by(Event.processing_lease_expires_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            for event in events:
                self._finalise_interrupted(session, event, now)
                logger.warning(
                    "recovered interrupted delivery attempt", extra={"event_id": str(event.id)}
                )
            return len(events)

    def dead_letter_from_queue(self, message: DeliveryMessage) -> HandleResult:
        """The broker gave up redelivering this message (its dead-letter policy fired)."""
        now = self._clock()
        with session_scope(self._session_factory) as session:
            event = self._lock(session, message.event_id)
            if event is None:
                return HandleResult(Ack.ACK, "unknown_event")
            status = EventStatus(event.status)
            if status is EventStatus.PROCESSING and self._is_current_attempt(event, message):
                lease = event.processing_lease_expires_at
                if lease is not None and lease > now:
                    # A worker is still inside its lease. Do not pre-empt it: it
                    # will finish, or the lease reaper will recover the event.
                    return HandleResult(Ack.ACK, "in_progress_elsewhere")
                self._finalise_interrupted(session, event, now)
                return HandleResult(Ack.ACK, "recovered_interrupted_attempt")
            if status in CLAIMABLE and self._is_next_attempt(event, message):
                self._dead_letter(
                    session, event, now,
                    reason="queue_delivery_exhausted",
                    category=FailureCategory.QUEUE_EXHAUSTED,
                    message="the message queue exhausted its delivery attempts before a worker "
                    "could process this event",
                )  # fmt: skip
                return HandleResult(Ack.ACK, "dead_lettered")
            return HandleResult(Ack.ACK, "stale_message")

    # -- step 1: claim ------------------------------------------------------

    @staticmethod
    def _lock(session: Session, event_id: uuid.UUID) -> Event | None:
        return session.execute(
            select(Event).where(Event.id == event_id).with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def _is_next_attempt(event: Event, message: DeliveryMessage) -> bool:
        return (message.cycle, message.attempt) == (event.delivery_cycle, event.attempt_count + 1)

    @staticmethod
    def _is_current_attempt(event: Event, message: DeliveryMessage) -> bool:
        return (message.cycle, message.attempt) == (event.delivery_cycle, event.attempt_count)

    def _claim(self, message: DeliveryMessage) -> _Claimed | HandleResult:
        now = self._clock()
        with session_scope(self._session_factory) as session:
            event = self._lock(session, message.event_id)
            if event is None or event.workspace_id != message.workspace_id:
                return HandleResult(Ack.ACK, "unknown_event")
            status = EventStatus(event.status)

            if status is EventStatus.PROCESSING:
                if not self._is_current_attempt(event, message):
                    return HandleResult(Ack.ACK, "stale_message")
                lease = event.processing_lease_expires_at
                if lease is not None and lease > now:
                    # Another worker holds this attempt. Ask the broker to come
                    # back; by then it has finished or its lease has lapsed.
                    return HandleResult(Ack.NACK, "in_progress_elsewhere")
                self._finalise_interrupted(session, event, now)
                return HandleResult(Ack.ACK, "recovered_interrupted_attempt")

            if status not in CLAIMABLE or not self._is_next_attempt(event, message):
                # Redelivered or out-of-date message: the work is already done
                # or superseded.
                return HandleResult(Ack.ACK, "duplicate_message")

            integration = session.get_one(Integration, event.integration_id)
            if integration.status == "paused":
                transition(
                    session,
                    event,
                    EventStatus.HELD,
                    reason="integration_paused",
                    actor=WORKER,
                    now=now,
                )
                return HandleResult(Ack.ACK, "held")
            if integration.status != "active":
                self._dead_letter(
                    session, event, now,
                    reason="integration_disabled",
                    category=FailureCategory.INTEGRATION_DISABLED,
                    message="the integration was disabled before this event could be delivered",
                )  # fmt: skip
                return HandleResult(Ack.ACK, "integration_disabled")
            if event.payload_purged_at is not None:
                self._dead_letter(
                    session, event, now,
                    reason="payload_purged",
                    category=FailureCategory.CONFIGURATION,
                    message="the stored payload was removed by the retention policy",
                )  # fmt: skip
                return HandleResult(Ack.ACK, "payload_purged")

            transition(
                session, event, EventStatus.PROCESSING, reason="claimed", actor=WORKER, now=now,
                details={"attempt": message.attempt, "cycle": message.cycle},
            )  # fmt: skip
            version = session.get_one(IntegrationVersion, event.integration_version_id)
            config, config_issues = parse_config(version.config)
            if config is None:
                self._validation_failed(
                    session, event, now, "configuration", [i.as_dict() for i in config_issues]
                )
                return HandleResult(Ack.ACK, "validation_failed")

            outcome = run_pipeline(event.payload, config)
            if outcome.kind == "validation_failed":
                assert outcome.stage is not None
                self._validation_failed(
                    session, event, now, outcome.stage.value, [i.as_dict() for i in outcome.issues]
                )
                return HandleResult(Ack.ACK, "validation_failed")
            if outcome.kind == "filtered":
                transition(
                    session, event, EventStatus.FILTERED, reason="filtered_out", actor=WORKER, now=now,
                    details={"filter": outcome.filter_reason},
                )  # fmt: skip
                return HandleResult(Ack.ACK, "filtered")

            assert outcome.output is not None
            event.transformed_payload = outcome.output
            destination = session.get(Connection, integration.destination_connection_id)
            if destination is None or not destination.is_active:
                self._dead_letter(
                    session, event, now,
                    reason="destination_unavailable",
                    category=FailureCategory.CONFIGURATION,
                    message="the destination connection is missing or inactive",
                )  # fmt: skip
                return HandleResult(Ack.ACK, "destination_inactive")

            adapter = self._registry.destination(destination.adapter_type)
            secret = (
                self._secrets.resolve(destination.secret_ref) if destination.secret_ref else None
            )
            timeout = config.delivery.timeout_seconds
            replay_id = None
            if event.delivery_cycle > 1:
                replay_id = session.execute(
                    select(ReplayOperation.id).where(
                        ReplayOperation.event_id == event.id,
                        ReplayOperation.cycle == event.delivery_cycle,
                    )
                ).scalar_one_or_none()
            attempt = DeliveryAttempt(
                id=uuid.uuid4(),
                event_id=event.id,
                workspace_id=event.workspace_id,
                integration_version_id=version.id,
                replay_operation_id=replay_id,
                cycle=message.cycle,
                attempt_number=message.attempt,
                status="in_progress",
                # Stable across retries and replays so a destination that
                # honours it never applies the same event twice.
                idempotency_key=str(event.id),
                correlation_id=event.correlation_id,
                started_at=now,
            )
            session.add(attempt)
            event.attempt_count = message.attempt
            event.processing_lease_expires_at = (
                now + timedelta(seconds=timeout) + self._lease_margin
            )
            return _Claimed(
                attempt_id=attempt.id,
                adapter=adapter,
                destination_config=dict(destination.config),
                secret=secret,
                request=DeliveryRequest(
                    event_id=event.id,
                    workspace_id=event.workspace_id,
                    payload=outcome.output,
                    idempotency_key=attempt.idempotency_key,
                    correlation_id=event.correlation_id,
                    attempt_number=message.attempt,
                    timeout_seconds=timeout,
                ),
            )

    # -- step 3: complete ---------------------------------------------------

    def _complete(
        self, message: DeliveryMessage, attempt_id: uuid.UUID, result: DeliveryResult
    ) -> HandleResult:
        now = self._clock()
        with session_scope(self._session_factory) as session:
            event = self._lock(session, message.event_id)
            attempt = session.get(DeliveryAttempt, attempt_id)
            if event is None or attempt is None:
                return HandleResult(Ack.ACK, "unknown_event")
            if attempt.status != "in_progress" or event.status != EventStatus.PROCESSING.value:
                # Our lease lapsed and the reaper already closed this attempt as
                # interrupted and scheduled the next one. Keep that history.
                logger.warning("attempt was finalised elsewhere; discarding late result")
                return HandleResult(Ack.ACK, "attempt_already_finalised")
            self._record_result(session, event, attempt, result, now)
            return HandleResult(Ack.ACK, event.status.lower())

    def _record_result(
        self,
        session: Session,
        event: Event,
        attempt: DeliveryAttempt,
        result: DeliveryResult,
        now: datetime,
    ) -> None:
        attempt.finished_at = now
        attempt.duration_ms = result.duration_ms
        attempt.disposition = result.disposition.value
        attempt.failure_category = (
            result.failure_category.value if result.failure_category else None
        )
        attempt.http_status = result.http_status
        attempt.request_method = result.request_method
        attempt.request_url = result.request_url
        attempt.response_summary = result.response_summary
        attempt.retry_after_seconds = result.retry_after_seconds
        attempt.error_message = result.error_message

        if result.disposition is Disposition.SUCCESS:
            attempt.status = "succeeded"
            transition(
                session, event, EventStatus.DELIVERED, reason="delivered", actor=WORKER, now=now,
                details={"attempt": attempt.attempt_number, "http_status": result.http_status},
            )  # fmt: skip
            return

        attempt.status = "failed"
        event.failure_stage = "delivery"
        event.failure_category = attempt.failure_category
        event.last_error = result.error_message
        event.failure_details = None
        decision = decide_retry(
            disposition=result.disposition,
            attempt_number=attempt.attempt_number,
            policy=self._retry_policy(session, event),
            rng=self._rng,
            retry_after_seconds=result.retry_after_seconds,
        )
        details = {
            "attempt": attempt.attempt_number,
            "failure_category": attempt.failure_category,
            "http_status": result.http_status,
        }
        if decision.retry:
            next_at = now + timedelta(seconds=decision.delay_seconds)
            transition(
                session, event, EventStatus.RETRY_SCHEDULED, reason="retry_scheduled", actor=WORKER, now=now,
                details={**details, "delay_seconds": decision.delay_seconds},
            )  # fmt: skip
            event.next_attempt_at = next_at
            schedule_delivery(
                session, event, attempt_number=attempt.attempt_number + 1, available_at=next_at
            )
        else:
            transition(
                session, event, EventStatus.DEAD_LETTERED, reason=decision.reason, actor=WORKER, now=now,
                details=details,
            )  # fmt: skip

    def _retry_policy(self, session: Session, event: Event) -> RetryPolicy:
        version = session.get(IntegrationVersion, event.integration_version_id)
        config: IntegrationConfig | None = None
        if version is not None:
            config, _ = parse_config(version.config)
        base = config.retry if config is not None else RetryPolicy()
        # max_attempts was snapshotted on the event so the budget for an
        # in-flight event cannot change underneath it.
        return base.model_copy(update={"max_attempts": max(1, min(event.max_attempts, 10))})

    # -- shared outcomes ----------------------------------------------------

    def _finalise_interrupted(self, session: Session, event: Event, now: datetime) -> None:
        attempt = session.execute(
            select(DeliveryAttempt).where(
                DeliveryAttempt.event_id == event.id,
                DeliveryAttempt.cycle == event.delivery_cycle,
                DeliveryAttempt.attempt_number == event.attempt_count,
            )
        ).scalar_one_or_none()
        if attempt is None or attempt.status != "in_progress":
            # PROCESSING with no open attempt should not happen; fail safe.
            self._dead_letter(
                session, event, now,
                reason="inconsistent_state",
                category=FailureCategory.WORKER_INTERRUPTED,
                message="processing was interrupted and no open attempt was found",
            )  # fmt: skip
            return
        started = attempt.started_at
        result = DeliveryResult(
            disposition=Disposition.RETRYABLE,
            duration_ms=max(0, int((now - started).total_seconds() * 1000)),
            failure_category=FailureCategory.WORKER_INTERRUPTED,
            error_message=INTERRUPTED_MESSAGE,
        )
        self._record_result(session, event, attempt, result, now)

    def _validation_failed(
        self,
        session: Session,
        event: Event,
        now: datetime,
        stage: str,
        issues: list[dict[str, str]],
    ) -> None:
        event.failure_stage = stage
        event.failure_category = None
        event.failure_details = issues
        event.last_error = f"{len(issues)} validation error(s) at stage '{stage}'"
        transition(
            session, event, EventStatus.VALIDATION_FAILED, reason="validation_failed", actor=WORKER, now=now,
            details={"stage": stage, "issue_count": len(issues)},
        )  # fmt: skip

    def _dead_letter(
        self,
        session: Session,
        event: Event,
        now: datetime,
        *,
        reason: str,
        category: FailureCategory,
        message: str,
        actor: Actor = WORKER,
    ) -> None:
        event.failure_stage = "delivery"
        event.failure_category = category.value
        event.last_error = message
        transition(
            session, event, EventStatus.DEAD_LETTERED, reason=reason, actor=actor, now=now,
            details={"failure_category": category.value},
        )  # fmt: skip
        cancel_pending(session, event.id)
