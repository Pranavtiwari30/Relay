"""Development broker backed by PostgreSQL.

Stands in for Pub/Sub when ``RELAY_QUEUE_BACKEND=local`` so the full pipeline
runs with no cloud account, emulator, Java or Docker. It reproduces the Pub/Sub
semantics the worker relies on: durable accept on publish, HTTP push of the same
envelope, redelivery with backoff on any non-2xx response, a delivery-attempt
counter, and hand-off to a dead-letter endpoint after a bounded number of tries.

It is not used in the cloud profile and makes no throughput claims.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from relay_core.db.models import LocalQueueMessage
from relay_core.db.session import SessionFactory, session_scope, utcnow
from relay_core.messaging.base import PublishError, encode_push_envelope

logger = logging.getLogger(__name__)

SUBSCRIPTION = "local/subscriptions/relay-deliveries-push"
DEAD_LETTER_SUBSCRIPTION = "local/subscriptions/relay-deliveries-dead-letter-push"

# (path, envelope) -> HTTP status code. Raising counts as a failed delivery.
PushTransport = Callable[[str, dict[str, Any]], int]

PUSH_PATH = "/internal/pubsub/push"
DEAD_LETTER_PATH = "/internal/pubsub/dead-letter"


class LocalQueuePublisher:
    def __init__(
        self, session_factory: SessionFactory, topic: str, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._session_factory = session_factory
        self._topic = topic
        self._clock = clock

    def publish(self, data: dict[str, Any], attributes: dict[str, str]) -> str:
        message_id = uuid.uuid4()
        try:
            # Its own transaction: like a real broker, "accepted" is independent
            # of whatever transaction the caller is in.
            with session_scope(self._session_factory) as session:
                now = self._clock()
                session.add(
                    LocalQueueMessage(
                        id=message_id,
                        topic=self._topic,
                        data=data,
                        attributes=attributes,
                        available_at=now,
                        created_at=now,
                    )
                )
        except Exception as exc:
            raise PublishError(f"local queue rejected the message ({type(exc).__name__})") from exc
        return str(message_id)


@dataclass
class PumpStats:
    delivered: int = 0
    redelivery_scheduled: int = 0
    dead_lettered: int = 0


class LocalQueuePump:
    """Pushes ready messages to the worker, mimicking a Pub/Sub push subscription."""

    def __init__(
        self,
        session_factory: SessionFactory,
        transport: PushTransport,
        *,
        topic: str,
        max_delivery_attempts: int = 5,
        ack_deadline_seconds: int = 60,
        batch_size: int = 20,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._topic = topic
        self._max_delivery_attempts = max_delivery_attempts
        self._ack_deadline = timedelta(seconds=ack_deadline_seconds)
        self._batch_size = batch_size
        self._clock = clock

    def _claim(self) -> list[LocalQueueMessage]:
        now = self._clock()
        with session_scope(self._session_factory) as session:
            rows = list(
                session.execute(
                    select(LocalQueueMessage)
                    .where(
                        LocalQueueMessage.topic == self._topic,
                        LocalQueueMessage.status == "ready",
                        LocalQueueMessage.available_at <= now,
                        (LocalQueueMessage.lease_expires_at.is_(None))
                        | (LocalQueueMessage.lease_expires_at < now),
                    )
                    .order_by(LocalQueueMessage.available_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            for row in rows:
                row.delivery_attempts += 1
                row.lease_expires_at = now + self._ack_deadline
            session.flush()
            for row in rows:
                session.expunge(row)
        return rows

    def _push(
        self, path: str, row: LocalQueueMessage, subscription: str
    ) -> tuple[bool, str | None]:
        envelope = encode_push_envelope(
            row.data,
            dict(row.attributes),
            message_id=str(row.id),
            subscription=subscription,
            delivery_attempt=row.delivery_attempts,
        )
        try:
            status = self._transport(path, envelope)
        except Exception as exc:
            return False, f"push failed ({type(exc).__name__})"
        if 200 <= status < 300:
            return True, None
        return False, f"push endpoint returned HTTP {status}"

    def tick(self) -> PumpStats:
        stats = PumpStats()
        for row in self._claim():
            acked, error = self._push(PUSH_PATH, row, SUBSCRIPTION)
            if acked:
                self._finish(row.id, delete=True)
                stats.delivered += 1
            elif row.delivery_attempts >= self._max_delivery_attempts:
                # Same hand-off Pub/Sub performs via a dead-letter topic.
                forwarded, _ = self._push(DEAD_LETTER_PATH, row, DEAD_LETTER_SUBSCRIPTION)
                if forwarded:
                    self._finish(row.id, delete=False, error=error, dead=True)
                    stats.dead_lettered += 1
                    logger.warning(
                        "local queue message dead-lettered", extra={"message_id": str(row.id)}
                    )
                else:
                    self._finish(row.id, delete=False, error=error, retry_in=30.0)
                    stats.redelivery_scheduled += 1
            else:
                delay = min(60.0, 2.0 ** (row.delivery_attempts - 1))
                self._finish(row.id, delete=False, error=error, retry_in=delay)
                stats.redelivery_scheduled += 1
        return stats

    def _finish(
        self,
        message_id: uuid.UUID,
        *,
        delete: bool,
        error: str | None = None,
        retry_in: float | None = None,
        dead: bool = False,
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(LocalQueueMessage, message_id)
            if row is None:
                return
            if delete:
                session.delete(row)
                return
            row.last_error = error
            row.lease_expires_at = None
            if dead:
                row.status = "dead"
            elif retry_in is not None:
                row.available_at = self._clock() + timedelta(seconds=retry_in)
