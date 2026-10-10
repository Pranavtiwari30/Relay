"""Integration lifecycle: versioned configuration, validation and status changes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from relay_core.adapters.registry import AdapterRegistry
from relay_core.db.models import Connection, Event, Integration, IntegrationVersion
from relay_core.domain.redaction import redact
from relay_core.domain.retry import FailureCategory
from relay_core.domain.states import EventStatus
from relay_core.domain.transform.models import IntegrationConfig
from relay_core.domain.transform.pipeline import (
    has_errors,
    parse_config,
    run_pipeline,
    validate_config,
)
from relay_core.errors import ConflictError, Issue, NotFoundError, ValidationFailedError
from relay_core.services.audit import record_audit
from relay_core.services.connections import validate_connection
from relay_core.services.event_state import Actor, transition
from relay_core.services.outbox import schedule_delivery
from relay_core.services.secrets import SecretResolver

StatusAction = Literal["activate", "pause", "resume", "disable"]

# action -> (statuses it is allowed from, resulting status)
_STATUS_RULES: dict[str, tuple[frozenset[str], str]] = {
    "activate": (frozenset({"draft", "disabled"}), "active"),
    "pause": (frozenset({"active"}), "paused"),
    "resume": (frozenset({"paused"}), "active"),
    "disable": (frozenset({"draft", "active", "paused"}), "disabled"),
}


def get_integration(
    session: Session, workspace_id: uuid.UUID, integration_id: uuid.UUID
) -> Integration:
    integration = session.execute(
        select(Integration).where(
            Integration.id == integration_id, Integration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if integration is None:
        raise NotFoundError("integration not found")
    return integration


def list_integrations(session: Session, workspace_id: uuid.UUID) -> list[Integration]:
    return list(
        session.execute(
            select(Integration)
            .where(Integration.workspace_id == workspace_id)
            .order_by(Integration.name)
        ).scalars()
    )


def list_versions(
    session: Session, workspace_id: uuid.UUID, integration_id: uuid.UUID
) -> list[IntegrationVersion]:
    get_integration(session, workspace_id, integration_id)
    return list(
        session.execute(
            select(IntegrationVersion)
            .where(IntegrationVersion.integration_id == integration_id)
            .order_by(IntegrationVersion.version.desc())
        ).scalars()
    )


def get_version(
    session: Session, workspace_id: uuid.UUID, version_id: uuid.UUID
) -> IntegrationVersion:
    version = session.execute(
        select(IntegrationVersion).where(
            IntegrationVersion.id == version_id, IntegrationVersion.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("integration version not found")
    return version


def _require_config(raw: Any) -> IntegrationConfig:
    config, issues = parse_config(raw)
    if config is None:
        raise ValidationFailedError.from_issues("integration configuration is not valid", issues)
    return config


def _connection(
    session: Session, workspace_id: uuid.UUID, connection_id: uuid.UUID, direction: str
) -> Connection:
    connection = session.execute(
        select(Connection).where(
            Connection.id == connection_id, Connection.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if connection is None or connection.direction != direction:
        raise ValidationFailedError(
            f"{direction} connection not found in this workspace",
            details=[
                Issue(
                    "invalid_connection",
                    f"{direction}_connection_id",
                    f"choose an existing {direction} connection",
                ).as_dict()
            ],
        )
    return connection


def activation_issues(
    session: Session,
    integration: Integration,
    config: IntegrationConfig,
    registry: AdapterRegistry,
    secrets: SecretResolver,
) -> list[Issue]:
    """Everything that must hold before an integration may process events."""
    issues = validate_config(config)
    for role, connection_id in (
        ("source", integration.source_connection_id),
        ("destination", integration.destination_connection_id),
    ):
        connection = session.get(Connection, connection_id)
        where = f"{role}_connection"
        if connection is None:
            issues.append(Issue("invalid_connection", where, f"{role} connection no longer exists"))
            continue
        if not connection.is_active:
            issues.append(
                Issue(
                    "connection_inactive",
                    where,
                    f"{role} connection '{connection.name}' is inactive",
                )
            )
        for issue in validate_connection(
            registry,
            direction=connection.direction,
            adapter_type=connection.adapter_type,
            config=connection.config,
            secret_ref=connection.secret_ref,
        ):
            issues.append(Issue(issue.code, f"{where}.{issue.path}", issue.message, issue.severity))
        if connection.secret_ref is not None and secrets.resolve(connection.secret_ref) is None:
            issues.append(
                Issue(
                    "secret_unavailable",
                    f"{where}.secret_ref",
                    f"secret reference '{connection.secret_ref}' is not set in this environment",
                )
            )
    return issues


def _next_version(session: Session, integration_id: uuid.UUID) -> int:
    current = session.execute(
        select(func.max(IntegrationVersion.version)).where(
            IntegrationVersion.integration_id == integration_id
        )
    ).scalar_one()
    return int(current or 0) + 1


def create_integration(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    name: str,
    description: str,
    source_connection_id: uuid.UUID,
    destination_connection_id: uuid.UUID,
    config: Any,
    change_note: str = "",
) -> Integration:
    parsed = _require_config(config)
    _connection(session, workspace_id, source_connection_id, "source")
    _connection(session, workspace_id, destination_connection_id, "destination")
    integration = Integration(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        description=description,
        source_connection_id=source_connection_id,
        destination_connection_id=destination_connection_id,
        status="draft",
        created_by=actor.user_id,
    )
    session.add(integration)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            f"an integration named '{name}' already exists", code="name_taken"
        ) from exc
    version = IntegrationVersion(
        id=uuid.uuid4(),
        integration_id=integration.id,
        workspace_id=workspace_id,
        version=1,
        config=parsed.to_storage(),
        change_note=change_note or "Initial version",
        created_by=actor.user_id,
    )
    session.add(version)
    session.flush()
    integration.current_version_id = version.id
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="integration.create",
        entity_type="integration",
        entity_id=integration.id,
        details={"name": name, "version": 1},
    )
    return integration


def update_integration(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    integration_id: uuid.UUID,
    registry: AdapterRegistry,
    secrets: SecretResolver,
    name: str | None = None,
    description: str | None = None,
    source_connection_id: uuid.UUID | None = None,
    destination_connection_id: uuid.UUID | None = None,
    config: Any = None,
    change_note: str = "",
) -> Integration:
    integration = session.execute(
        select(Integration)
        .where(Integration.id == integration_id, Integration.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if integration is None:
        raise NotFoundError("integration not found")
    changed: list[str] = []
    if name is not None and name != integration.name:
        integration.name = name
        changed.append("name")
    if description is not None and description != integration.description:
        integration.description = description
        changed.append("description")
    if (
        source_connection_id is not None
        and source_connection_id != integration.source_connection_id
    ):
        _connection(session, workspace_id, source_connection_id, "source")
        integration.source_connection_id = source_connection_id
        changed.append("source_connection")
    if (
        destination_connection_id is not None
        and destination_connection_id != integration.destination_connection_id
    ):
        _connection(session, workspace_id, destination_connection_id, "destination")
        integration.destination_connection_id = destination_connection_id
        changed.append("destination_connection")

    details: dict[str, Any] = {}
    current = (
        session.get(IntegrationVersion, integration.current_version_id)
        if integration.current_version_id
        else None
    )
    parsed = _require_config(config) if config is not None else None
    if parsed is not None and (current is None or parsed.to_storage() != current.config):
        if integration.status in ("active", "paused"):
            # A live integration may never point at a configuration that could
            # not have been activated.
            issues = activation_issues(session, integration, parsed, registry, secrets)
            if has_errors(issues):
                raise ValidationFailedError.from_issues(
                    "this configuration cannot be applied to a live integration",
                    [issue for issue in issues if issue.severity == "error"],
                )
        version = IntegrationVersion(
            id=uuid.uuid4(),
            integration_id=integration.id,
            workspace_id=workspace_id,
            version=_next_version(session, integration.id),
            config=parsed.to_storage(),
            change_note=change_note,
            created_by=actor.user_id,
        )
        session.add(version)
        session.flush()
        details["from_version"] = current.version if current else None
        details["to_version"] = version.version
        integration.current_version_id = version.id
        changed.append("config")
    if changed:
        try:
            session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                f"an integration named '{name}' already exists", code="name_taken"
            ) from exc
        record_audit(
            session,
            workspace_id=workspace_id,
            actor=actor,
            action="integration.update",
            entity_type="integration",
            entity_id=integration.id,
            details={"changed": changed, **details, "change_note": change_note[:200]},
        )
    return integration


def change_status(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    integration_id: uuid.UUID,
    action: StatusAction,
    registry: AdapterRegistry,
    secrets: SecretResolver,
    now: datetime,
) -> tuple[Integration, int]:
    """Apply a status action. Returns the integration and the number of events affected."""
    integration = session.execute(
        select(Integration)
        .where(Integration.id == integration_id, Integration.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if integration is None:
        raise NotFoundError("integration not found")
    allowed_from, target = _STATUS_RULES[action]
    if integration.status not in allowed_from:
        raise ConflictError(
            f"cannot {action} an integration that is {integration.status}",
            code="invalid_status_change",
        )
    previous = integration.status
    affected = 0
    if action in ("activate", "resume"):
        version = (
            session.get(IntegrationVersion, integration.current_version_id)
            if integration.current_version_id
            else None
        )
        config = _require_config(version.config if version else None)
        issues = activation_issues(session, integration, config, registry, secrets)
        if has_errors(issues):
            raise ValidationFailedError.from_issues(
                f"integration cannot be {'activated' if action == 'activate' else 'resumed'} until these are fixed",
                [issue for issue in issues if issue.severity == "error"],
            )
    integration.status = target
    if action in ("activate", "resume", "disable"):
        held = list(
            session.execute(
                select(Event)
                .where(
                    Event.integration_id == integration.id, Event.status == EventStatus.HELD.value
                )
                .order_by(Event.received_at)
                .with_for_update()
            ).scalars()
        )
        for event in held:
            if action == "disable":
                event.failure_stage = "delivery"
                event.failure_category = FailureCategory.INTEGRATION_DISABLED.value
                event.last_error = "the integration was disabled while this event was held"
                transition(
                    session,
                    event,
                    EventStatus.DEAD_LETTERED,
                    reason="integration_disabled",
                    actor=actor,
                    now=now,
                )
            else:
                transition(
                    session,
                    event,
                    EventStatus.PENDING_PUBLISH,
                    reason="integration_resumed",
                    actor=actor,
                    now=now,
                )
                schedule_delivery(
                    session, event, attempt_number=event.attempt_count + 1, available_at=now
                )
        affected = len(held)
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action=f"integration.{action}",
        entity_type="integration",
        entity_id=integration.id,
        details={"from_status": previous, "to_status": target, "held_events_affected": affected},
    )
    return integration, affected


def preview_transformation(config: Any, payload: Any) -> dict[str, Any]:
    """Dry-run the pipeline for the editor. Pure: nothing is stored or delivered."""
    parsed, issues = parse_config(config)
    if parsed is None:
        return {
            "kind": "invalid_config",
            "stage": "configuration",
            "issues": [i.as_dict() for i in issues],
            "output": None,
        }
    static = [issue for issue in validate_config(parsed) if issue.severity == "error"]
    if static:
        return {
            "kind": "invalid_config",
            "stage": "configuration",
            "issues": [i.as_dict() for i in static],
            "output": None,
        }
    result = run_pipeline(payload, parsed)
    return {
        "kind": result.kind,
        "stage": result.stage.value if result.stage else None,
        "issues": [issue.as_dict() for issue in result.issues],
        "output": redact(result.output, extra_keys=parsed.redact_fields)
        if result.output is not None
        else None,
        "filter_reason": result.filter_reason,
    }
