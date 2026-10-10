"""Webhook ingestion: authenticate, persist, and enqueue — in that order.

The 2xx acknowledgement is sent only after the event row and its outbox row
have committed in one transaction. Delivery then happens asynchronously.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from relay_core.adapters.base import InboundRequest
from relay_core.adapters.registry import AdapterRegistry
from relay_core.adapters.webhook_source import require_secret
from relay_core.config import Settings
from relay_core.db.models import Connection, Event, Integration, IntegrationVersion
from relay_core.db.session import SessionFactory, session_scope, utcnow
from relay_core.domain.states import EventStatus
from relay_core.domain.transform.pipeline import parse_config, validate_input
from relay_core.errors import (
    BadRequestError,
    Issue,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    UnauthenticatedError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from relay_core.logging import bind_log_context
from relay_core.services import ratelimit
from relay_core.services.event_state import INGESTION, record_received, transition
from relay_core.services.outbox import OutboxDispatcher, schedule_delivery
from relay_core.services.secrets import SecretResolver

logger = logging.getLogger(__name__)

Outcome = Literal["accepted", "duplicate", "validation_failed", "held"]


@dataclass(frozen=True)
class IngestResult:
    integration_id: uuid.UUID
    event_id: uuid.UUID
    outcome: Outcome
    issues: tuple[Issue, ...] = ()


@dataclass
class IngestResponse:
    correlation_id: str
    results: list[IngestResult] = field(default_factory=list)

    @property
    def all_rejected(self) -> bool:
        return bool(self.results) and all(r.outcome == "validation_failed" for r in self.results)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _contains_nul(value: Any) -> bool:
    # PostgreSQL jsonb cannot store U+0000; reject it here with a clear error
    # rather than failing inside the insert.
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            for key, child in item.items():
                if "\x00" in key:
                    return True
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return False


def parse_json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except UnicodeDecodeError as exc:
        raise BadRequestError("request body is not valid UTF-8", code="invalid_json") from exc
    except (ValueError, RecursionError) as exc:
        raise BadRequestError("request body is not valid JSON", code="invalid_json") from exc
    if not isinstance(payload, dict):
        raise BadRequestError("request body must be a JSON object", code="invalid_json")
    if _contains_nul(payload):
        raise BadRequestError(
            "request body contains unsupported NUL characters", code="invalid_json"
        )
    return payload


def compute_dedup_key(source_event_id: str | None, body: bytes) -> str:
    """Stable identity for deduplication: the source's event id, else the exact body."""
    if source_event_id is not None:
        return "i:" + hashlib.sha256(b"id\x00" + source_event_id.encode()).hexdigest()
    return "b:" + hashlib.sha256(b"body\x00" + body).hexdigest()


class IngestionService:
    def __init__(
        self,
        session_factory: SessionFactory,
        registry: AdapterRegistry,
        secrets: SecretResolver,
        settings: Settings,
        *,
        dispatcher: OutboxDispatcher | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._secrets = secrets
        self._settings = settings
        self._dispatcher = dispatcher
        self._clock = clock

    def ingest(
        self, connection_id: uuid.UUID, request: InboundRequest, *, correlation_id: str
    ) -> IngestResponse:
        with bind_log_context(connection_id=str(connection_id)):
            return self._ingest(connection_id, request, correlation_id)

    def _rate_limit(self, connection_id: uuid.UUID, now: datetime) -> None:
        decision = ratelimit.hit(
            self._session_factory,
            f"webhook:{connection_id}",
            limit=self._settings.webhook_rate_limit_per_minute,
            window_seconds=60,
            now=now,
        )
        if not decision.allowed:
            raise RateLimitedError(
                "too many requests for this webhook endpoint",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

    def _ingest(
        self, connection_id: uuid.UUID, request: InboundRequest, correlation_id: str
    ) -> IngestResponse:
        now = self._clock()
        with self._session_factory() as session:
            connection = session.execute(
                select(Connection).where(
                    Connection.id == connection_id,
                    Connection.direction == "source",
                    Connection.is_active.is_(True),
                )
            ).scalar_one_or_none()
        if connection is None:
            raise NotFoundError("unknown webhook endpoint", code="unknown_endpoint")

        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise UnsupportedMediaTypeError("Content-Type must be application/json")
        if len(request.body) > self._settings.max_webhook_body_bytes:
            raise PayloadTooLargeError(
                f"request body exceeds {self._settings.max_webhook_body_bytes} bytes"
            )

        adapter = self._registry.source(connection.adapter_type)
        secret = self._secrets.resolve(connection.secret_ref) if connection.secret_ref else None
        require_secret(secret, has_ref=bool(connection.secret_ref))
        # Signed endpoints are limited *after* authentication so that forged
        # traffic cannot spend a legitimate sender's allowance. Unsigned
        # endpoints have no such distinction and are limited up front.
        if secret is None:
            self._rate_limit(connection_id, now)
        try:
            adapter.authenticate(
                connection.config, secret, request,
                tolerance_seconds=self._settings.signature_tolerance_seconds,
            )  # fmt: skip
        except UnauthenticatedError as exc:
            logger.warning(
                "webhook rejected", extra={"reason": (exc.details or [{}])[0].get("reason")}
            )
            exc.details = []
            raise
        if secret is not None:
            self._rate_limit(connection_id, now)

        payload = parse_json_object(request.body)
        identity = adapter.identify(connection.config, request, payload)
        dedup_key = compute_dedup_key(identity.source_event_id, request.body)
        body_hash = hashlib.sha256(request.body).hexdigest()

        response = IngestResponse(correlation_id=correlation_id)
        outbox_ids: list[uuid.UUID] = []
        with session_scope(self._session_factory) as session:
            routes = session.execute(
                select(Integration, IntegrationVersion)
                .join(IntegrationVersion, IntegrationVersion.id == Integration.current_version_id)
                .where(
                    Integration.source_connection_id == connection_id,
                    Integration.status.in_(("active", "paused")),
                )
                .order_by(Integration.created_at)
            ).all()
            for integration, version in routes:
                accepted_types = version.config.get("event_types") or []
                if accepted_types and identity.event_type not in accepted_types:
                    continue
                result, outbox_id = self._persist(
                    session,
                    integration=integration,
                    version=version,
                    payload=payload,
                    body_size=len(request.body),
                    body_hash=body_hash,
                    dedup_key=dedup_key,
                    source_event_id=identity.source_event_id,
                    event_type=identity.event_type,
                    correlation_id=correlation_id,
                    now=now,
                )
                response.results.append(result)
                if outbox_id is not None:
                    outbox_ids.append(outbox_id)
            if not response.results:
                raise ValidationFailedError(
                    "no active integration accepts this event"
                    + (f" (event type '{identity.event_type}')" if identity.event_type else ""),
                    code="no_matching_integration",
                )

        for result in response.results:
            logger.info(
                "event ingested",
                extra={
                    "event_id": str(result.event_id),
                    "integration_id": str(result.integration_id),
                    "outcome": result.outcome,
                },
            )
        if self._dispatcher is not None and outbox_ids:
            # Fast path only. If this fails the rows stay pending and the
            # scheduled dispatcher publishes them.
            try:
                self._dispatcher.dispatch_ids(outbox_ids)
            except Exception:
                logger.warning("inline publish failed; dispatcher will retry", exc_info=True)
        return response

    def _persist(
        self,
        session: Session,
        *,
        integration: Integration,
        version: IntegrationVersion,
        payload: dict[str, Any],
        body_size: int,
        body_hash: str,
        dedup_key: str,
        source_event_id: str | None,
        event_type: str | None,
        correlation_id: str,
        now: datetime,
    ) -> tuple[IngestResult, uuid.UUID | None]:
        config, config_issues = parse_config(version.config)
        event_id = uuid.uuid4()
        inserted = session.execute(
            pg_insert(Event)
            .values(
                id=event_id,
                workspace_id=integration.workspace_id,
                integration_id=integration.id,
                integration_version_id=version.id,
                source_connection_id=integration.source_connection_id,
                source_event_id=source_event_id,
                dedup_key=dedup_key,
                event_type=event_type,
                status=EventStatus.RECEIVED.value,
                payload=payload,
                payload_sha256=body_hash,
                payload_size_bytes=body_size,
                correlation_id=correlation_id,
                max_attempts=config.retry.max_attempts if config else 1,
                received_at=now,
                updated_at=now,
            )
            # The unique constraint, not an in-memory check, is what makes
            # deduplication correct across concurrent requests and instances.
            .on_conflict_do_nothing(constraint="uq_events_integration_dedup")
            .returning(Event.id)
        ).scalar_one_or_none()
        if inserted is None:
            existing = session.execute(
                select(Event.id).where(
                    Event.integration_id == integration.id, Event.dedup_key == dedup_key
                )
            ).scalar_one()
            return IngestResult(integration.id, existing, "duplicate"), None

        event = session.get_one(Event, event_id)
        record_received(session, event, now=now)

        issues = config_issues if config is None else validate_input(payload, config)
        if issues:
            event.failure_stage = "configuration" if config is None else "input_schema"
            event.failure_details = [issue.as_dict() for issue in issues]
            event.last_error = f"{len(issues)} validation error(s) in the inbound payload"
            transition(
                session, event, EventStatus.VALIDATION_FAILED,
                reason="input_validation_failed", actor=INGESTION, now=now,
                details={"stage": event.failure_stage, "issue_count": len(issues)},
            )  # fmt: skip
            return IngestResult(integration.id, event_id, "validation_failed", tuple(issues)), None

        if integration.status == "paused":
            transition(
                session,
                event,
                EventStatus.HELD,
                reason="integration_paused",
                actor=INGESTION,
                now=now,
            )
            return IngestResult(integration.id, event_id, "held"), None

        transition(
            session, event, EventStatus.PENDING_PUBLISH, reason="accepted", actor=INGESTION, now=now
        )
        outbox_id = schedule_delivery(session, event, attempt_number=1, available_at=now)
        return IngestResult(integration.id, event_id, "accepted"), outbox_id
