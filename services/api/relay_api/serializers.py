"""ORM rows -> API response shapes. Payload redaction for display happens here."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relay_core.adapters.registry import AdapterRegistry
from relay_core.db.models import (
    AuditLog,
    Connection,
    DeliveryAttempt,
    Event,
    EventTransition,
    Integration,
    IntegrationVersion,
    Investigation,
    ReplayOperation,
)
from relay_core.domain.redaction import redact
from relay_core.domain.states import REPLAYABLE, EventStatus
from relay_core.services.connections import secret_is_resolvable, validate_connection
from relay_core.services.secrets import SecretResolver


def connection_out(
    connection: Connection, registry: AdapterRegistry, secrets: SecretResolver
) -> dict[str, Any]:
    try:
        issues = validate_connection(
            registry,
            direction=connection.direction,
            adapter_type=connection.adapter_type,
            config=connection.config,
            secret_ref=connection.secret_ref,
        )
    except Exception:
        issues = []
    return {
        "id": connection.id,
        "name": connection.name,
        "direction": connection.direction,
        "adapter_type": connection.adapter_type,
        "config": connection.config,
        "secret_ref": connection.secret_ref,
        "secret_resolvable": secret_is_resolvable(secrets, connection),
        "is_active": connection.is_active,
        "webhook_path": f"/webhooks/{connection.id}" if connection.direction == "source" else None,
        "warnings": [issue.as_dict() for issue in issues if issue.severity == "warning"],
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


def integrations_out(session: Session, integrations: Iterable[Integration]) -> list[dict[str, Any]]:
    """Serialise integrations with connection names and version numbers in two queries."""
    rows = list(integrations)
    connection_ids = {i.source_connection_id for i in rows} | {
        i.destination_connection_id for i in rows
    }
    version_ids = {i.current_version_id for i in rows if i.current_version_id}
    connections = (
        {
            c.id: c
            for c in session.execute(
                select(Connection).where(Connection.id.in_(connection_ids))
            ).scalars()
        }
        if connection_ids
        else {}
    )
    versions = (
        {
            row[0]: row[1]
            for row in session.execute(
                select(IntegrationVersion.id, IntegrationVersion.version).where(
                    IntegrationVersion.id.in_(version_ids)
                )
            ).all()
        }
        if version_ids
        else {}
    )
    out = []
    for item in rows:
        source = connections.get(item.source_connection_id)
        destination = connections.get(item.destination_connection_id)
        out.append(
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "status": item.status,
                "source_connection_id": item.source_connection_id,
                "source_connection_name": source.name if source else None,
                "destination_connection_id": item.destination_connection_id,
                "destination_connection_name": destination.name if destination else None,
                "destination_adapter_type": destination.adapter_type if destination else None,
                "current_version_id": item.current_version_id,
                "current_version": versions.get(item.current_version_id)
                if item.current_version_id
                else None,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
        )
    return out


def integration_detail(session: Session, integration: Integration) -> dict[str, Any]:
    base = integrations_out(session, [integration])[0]
    version = (
        session.get(IntegrationVersion, integration.current_version_id)
        if integration.current_version_id
        else None
    )
    return {**base, "config": version.config if version else {}}


def version_out(version: IntegrationVersion, current_id: uuid.UUID | None) -> dict[str, Any]:
    return {
        "id": version.id,
        "version": version.version,
        "config": version.config,
        "change_note": version.change_note,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "is_current": version.id == current_id,
    }


def integration_names(session: Session, events: Iterable[Event]) -> dict[uuid.UUID, str]:
    ids = {event.integration_id for event in events}
    if not ids:
        return {}
    return {
        row[0]: row[1]
        for row in session.execute(
            select(Integration.id, Integration.name).where(Integration.id.in_(ids))
        ).all()
    }


def event_summary(event: Event, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": event.id,
        "integration_id": event.integration_id,
        "integration_name": names.get(event.integration_id),
        "status": event.status,
        "event_type": event.event_type,
        "source_event_id": event.source_event_id,
        "correlation_id": event.correlation_id,
        "attempt_count": event.attempt_count,
        "max_attempts": event.max_attempts,
        "delivery_cycle": event.delivery_cycle,
        "next_attempt_at": event.next_attempt_at,
        "failure_stage": event.failure_stage,
        "failure_category": event.failure_category,
        "last_error": event.last_error,
        "received_at": event.received_at,
        "updated_at": event.updated_at,
        "delivered_at": event.delivered_at,
        "dead_lettered_at": event.dead_lettered_at,
    }


def transition_out(row: EventTransition) -> dict[str, Any]:
    return {
        "id": row.id,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "reason": row.reason,
        "actor_type": row.actor_type,
        "actor_user_id": row.actor_user_id,
        "details": row.details,
        "created_at": row.created_at,
    }


def attempt_out(row: DeliveryAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "cycle": row.cycle,
        "attempt_number": row.attempt_number,
        "status": row.status,
        "disposition": row.disposition,
        "failure_category": row.failure_category,
        "http_status": row.http_status,
        "request_method": row.request_method,
        "request_url": row.request_url,
        "response_summary": row.response_summary,
        "retry_after_seconds": row.retry_after_seconds,
        "error_message": row.error_message,
        "idempotency_key": row.idempotency_key,
        "integration_version_id": row.integration_version_id,
        "replay_operation_id": row.replay_operation_id,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "duration_ms": row.duration_ms,
    }


def replay_out(row: ReplayOperation) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "cycle": row.cycle,
        "version_mode": row.version_mode,
        "integration_version_id": row.integration_version_id,
        "from_status": row.from_status,
        "reason": row.reason,
        "requested_by": row.requested_by,
        "created_at": row.created_at,
    }


def event_detail(
    session: Session,
    event: Event,
    transitions: list[EventTransition],
    replays: list[ReplayOperation],
) -> dict[str, Any]:
    version = session.get(IntegrationVersion, event.integration_version_id)
    extra = list((version.config or {}).get("redact_fields") or []) if version else []
    return {
        **event_summary(event, integration_names(session, [event])),
        "integration_version_id": event.integration_version_id,
        "integration_version": version.version if version else None,
        # Always redacted: there is no endpoint that returns a raw payload.
        "payload": redact(event.payload, extra_keys=extra),
        "transformed_payload": redact(event.transformed_payload, extra_keys=extra)
        if event.transformed_payload is not None
        else None,
        "payload_size_bytes": event.payload_size_bytes,
        "payload_purged": event.payload_purged_at is not None,
        "failure_details": event.failure_details,
        "replayable": EventStatus(event.status) in REPLAYABLE and event.payload_purged_at is None,
        "transitions": [transition_out(t) for t in transitions],
        "replays": [replay_out(r) for r in replays],
    }


def investigation_out(row: Investigation) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "status": row.status,
        "mode": row.mode,
        "provider": row.provider,
        "model": row.model,
        "report": row.report,
        "evidence_refs": row.evidence_refs or [],
        "decisions": row.decisions or {},
        "fallback_reason": row.fallback_reason,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "trace": row.trace,
        "requested_by": row.requested_by,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }


def audit_out(row: AuditLog, actor_email: str | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "actor_type": row.actor_type,
        "actor_user_id": row.actor_user_id,
        "actor_email": actor_email,
        "details": row.details,
        "correlation_id": row.correlation_id,
        "created_at": row.created_at,
    }
