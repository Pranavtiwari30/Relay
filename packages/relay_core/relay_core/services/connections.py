"""Connection management: adapter configuration plus a secret *reference*."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from relay_core.adapters.registry import AdapterRegistry
from relay_core.db.models import Connection, Integration
from relay_core.errors import ConflictError, Issue, NotFoundError, ValidationFailedError
from relay_core.services.audit import record_audit
from relay_core.services.event_state import Actor
from relay_core.services.secrets import SecretResolver, validate_secret_ref

UNSET: Any = object()


def validate_connection(
    registry: AdapterRegistry,
    *,
    direction: str,
    adapter_type: str,
    config: dict[str, Any],
    secret_ref: str | None,
) -> list[Issue]:
    issues: list[Issue] = []
    if secret_ref is not None:
        reason = validate_secret_ref(secret_ref)
        if reason is not None:
            issues.append(Issue("invalid_secret_ref", "secret_ref", reason))
    if direction == "source":
        issues.extend(
            registry.source(adapter_type).validate_config(config, has_secret=secret_ref is not None)
        )
    elif direction == "destination":
        issues.extend(
            registry.destination(adapter_type).validate_config(
                config, has_secret=secret_ref is not None
            )
        )
    else:
        issues.append(Issue("invalid_direction", "direction", "must be 'source' or 'destination'"))
    return issues


def _raise_on_errors(issues: list[Issue]) -> None:
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ValidationFailedError.from_issues("connection configuration is not valid", errors)


def secret_is_resolvable(secrets: SecretResolver, connection: Connection) -> bool | None:
    """``None`` when no secret is referenced; otherwise whether it resolves here."""
    if connection.secret_ref is None:
        return None
    return secrets.resolve(connection.secret_ref) is not None


def list_connections(session: Session, workspace_id: uuid.UUID) -> list[Connection]:
    return list(
        session.execute(
            select(Connection)
            .where(Connection.workspace_id == workspace_id)
            .order_by(Connection.name)
        ).scalars()
    )


def get_connection(
    session: Session, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> Connection:
    connection = session.execute(
        select(Connection).where(
            Connection.id == connection_id, Connection.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if connection is None:
        raise NotFoundError("connection not found")
    return connection


def _name_taken(
    session: Session, workspace_id: uuid.UUID, name: str, exclude: uuid.UUID | None
) -> bool:
    query = (
        select(func.count())
        .select_from(Connection)
        .where(Connection.workspace_id == workspace_id, Connection.name == name)
    )
    if exclude is not None:
        query = query.where(Connection.id != exclude)
    return bool(session.execute(query).scalar_one())


def create_connection(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    registry: AdapterRegistry,
    name: str,
    direction: str,
    adapter_type: str,
    config: dict[str, Any],
    secret_ref: str | None,
) -> tuple[Connection, list[Issue]]:
    issues = validate_connection(
        registry,
        direction=direction,
        adapter_type=adapter_type,
        config=config,
        secret_ref=secret_ref,
    )
    _raise_on_errors(issues)
    if _name_taken(session, workspace_id, name, None):
        raise ConflictError(f"a connection named '{name}' already exists", code="name_taken")
    connection = Connection(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        direction=direction,
        adapter_type=adapter_type,
        config=config,
        secret_ref=secret_ref,
        created_by=actor.user_id,
    )
    session.add(connection)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            f"a connection named '{name}' already exists", code="name_taken"
        ) from exc
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="connection.create",
        entity_type="connection",
        entity_id=connection.id,
        details={
            "name": name,
            "direction": direction,
            "adapter_type": adapter_type,
            "has_secret": secret_ref is not None,
        },
    )
    return connection, issues


def update_connection(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    registry: AdapterRegistry,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    secret_ref: Any = UNSET,
    is_active: bool | None = None,
) -> tuple[Connection, list[Issue]]:
    connection = get_connection(session, workspace_id, connection_id)
    new_config = connection.config if config is None else config
    new_secret = connection.secret_ref if secret_ref is UNSET else secret_ref
    issues = validate_connection(
        registry,
        direction=connection.direction,
        adapter_type=connection.adapter_type,
        config=new_config,
        secret_ref=new_secret,
    )
    _raise_on_errors(issues)
    changed: list[str] = []
    if name is not None and name != connection.name:
        if _name_taken(session, workspace_id, name, connection.id):
            raise ConflictError(f"a connection named '{name}' already exists", code="name_taken")
        connection.name = name
        changed.append("name")
    if config is not None and config != connection.config:
        connection.config = config
        changed.append("config")
    if secret_ref is not UNSET and secret_ref != connection.secret_ref:
        connection.secret_ref = secret_ref
        changed.append("secret_ref")
    if is_active is not None and is_active != connection.is_active:
        connection.is_active = is_active
        changed.append("is_active")
    if changed:
        session.flush()
        record_audit(
            session,
            workspace_id=workspace_id,
            actor=actor,
            action="connection.update",
            entity_type="connection",
            entity_id=connection.id,
            # Field names only: never the secret reference's value or old config.
            details={"changed": changed, "is_active": connection.is_active},
        )
    return connection, issues


def delete_connection(
    session: Session, *, actor: Actor, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> None:
    connection = get_connection(session, workspace_id, connection_id)
    in_use = session.execute(
        select(func.count())
        .select_from(Integration)
        .where(
            (Integration.source_connection_id == connection.id)
            | (Integration.destination_connection_id == connection.id)
        )
    ).scalar_one()
    if in_use:
        raise ConflictError(
            f"connection is used by {in_use} integration(s) and cannot be deleted; deactivate it instead",
            code="connection_in_use",
        )
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="connection.delete",
        entity_type="connection",
        entity_id=connection.id,
        details={"name": connection.name},
    )
    session.delete(connection)
