"""Audit trail for configuration, permission and recovery actions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from relay_core.db.models import AuditLog
from relay_core.domain.redaction import redact
from relay_core.logging import correlation_id
from relay_core.services.event_state import Actor


def record_audit(
    session: Session,
    *,
    workspace_id: UUID | None,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: UUID | str,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Append an audit entry in the caller's transaction.

    Writing in the same transaction as the change means an action is never
    committed without its audit row. ``details`` is redacted as a backstop, but
    callers must pass identifiers and summaries only — never payloads or secrets.
    """
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_type=actor.type,
        actor_user_id=actor.user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=redact(details or {}),
        correlation_id=correlation_id(),
    )
    session.add(entry)
    return entry
