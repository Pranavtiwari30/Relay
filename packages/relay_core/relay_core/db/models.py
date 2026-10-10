"""SQLAlchemy models. Table purposes and index rationale: docs/architecture/data-model.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JsonDict = dict[str, Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        JsonDict: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), nullable=False)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


EVENT_STATUSES = (
    "RECEIVED", "HELD", "VALIDATION_FAILED", "FILTERED", "PENDING_PUBLISH", "QUEUED",
    "PROCESSING", "RETRY_SCHEDULED", "DELIVERED", "DEAD_LETTERED", "REPLAY_REQUESTED",
)  # fmt: skip


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[datetime | None]

    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created()


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        CheckConstraint(_in("role", ("admin", "operator", "viewer")), name="role_valid"),
    )


class UserSession(Base):
    """Server-side session. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
    user_agent: Mapped[str | None] = mapped_column(String(300))


class Connection(Base):
    """A source or destination endpoint. Holds safe config and a secret *reference* only."""

    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    direction: Mapped[str] = mapped_column(String(20))
    adapter_type: Mapped[str] = mapped_column(String(40))
    config: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(_in("direction", ("source", "destination")), name="direction_valid"),
    )


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, server_default="")
    source_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT")
    )
    destination_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), server_default="draft")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("integration_versions.id", use_alter=True, ondelete="RESTRICT")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(
            _in("status", ("draft", "active", "paused", "disabled")), name="status_valid"
        ),
        # Webhook routing: "which live integrations listen on this source connection?"
        Index("ix_integrations_source_routing", "source_connection_id", "status"),
        Index("ix_integrations_destination_connection_id", "destination_connection_id"),
    )


class IntegrationVersion(Base):
    """Immutable configuration revision. Rows are inserted, never updated."""

    __tablename__ = "integration_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[JsonDict]
    change_note: Mapped[str] = mapped_column(Text, server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()

    __table_args__ = (UniqueConstraint("integration_id", "version"),)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE")
    )
    integration_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_versions.id", ondelete="RESTRICT")
    )
    source_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT")
    )
    source_event_id: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24))
    payload: Mapped[JsonDict]
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_size_bytes: Mapped[int] = mapped_column(Integer)
    payload_purged_at: Mapped[datetime | None]
    transformed_payload: Mapped[JsonDict | None]
    correlation_id: Mapped[str] = mapped_column(String(64))
    delivery_cycle: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None]
    processing_lease_expires_at: Mapped[datetime | None]
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    failure_category: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_details: Mapped[list[Any] | None]
    received_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    delivered_at: Mapped[datetime | None]
    dead_lettered_at: Mapped[datetime | None]

    __table_args__ = (
        # Correctness boundary for deduplication across every API instance.
        UniqueConstraint("integration_id", "dedup_key", name="uq_events_integration_dedup"),
        CheckConstraint(_in("status", EVENT_STATUSES), name="status_valid"),
        CheckConstraint("attempt_count >= 0 AND delivery_cycle >= 1", name="counters_valid"),
        # Keyset pagination: every explorer query is workspace-scoped and
        # ordered by (received_at DESC, id DESC).
        Index("ix_events_ws_received", "workspace_id", text("received_at DESC"), text("id DESC")),
        Index(
            "ix_events_ws_status_received",
            "workspace_id",
            "status",
            text("received_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_events_ws_integration_received",
            "workspace_id",
            "integration_id",
            text("received_at DESC"),
            text("id DESC"),
        ),
        Index("ix_events_correlation_id", "correlation_id"),
        Index("ix_events_ws_source_event_id", "workspace_id", "source_event_id"),
        # Lease reaper: only ever scans rows that are mid-delivery.
        Index(
            "ix_events_processing_lease",
            "processing_lease_expires_at",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        Index("ix_events_held", "integration_id", postgresql_where=text("status = 'HELD'")),
    )


class EventTransition(Base):
    """Append-only state history. One row per status change."""

    __tablename__ = "event_transitions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(80))
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    details: Mapped[JsonDict | None]
    created_at: Mapped[datetime] = _created()

    __table_args__ = (Index("ix_event_transitions_event", "event_id", "id"),)


class OutboxMessage(Base):
    """Transactional outbox: one row per delivery attempt that must reach the broker."""

    __tablename__ = "outbox_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    cycle: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[JsonDict]
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    claim_token: Mapped[uuid.UUID | None]
    lease_expires_at: Mapped[datetime | None]
    publish_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    broker_message_id: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        # Scheduling a given attempt is idempotent.
        UniqueConstraint("event_id", "cycle", "attempt_number", name="uq_outbox_event_attempt"),
        CheckConstraint(_in("status", ("pending", "published", "cancelled")), name="status_valid"),
        # The dispatcher's claim query touches only due, pending rows.
        Index(
            "ix_outbox_pending_due",
            "available_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    integration_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_versions.id", ondelete="RESTRICT")
    )
    replay_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("replay_operations.id", ondelete="SET NULL")
    )
    cycle: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="in_progress")
    disposition: Mapped[str | None] = mapped_column(String(16))
    failure_category: Mapped[str | None] = mapped_column(String(40))
    http_status: Mapped[int | None] = mapped_column(Integer)
    request_method: Mapped[str | None] = mapped_column(String(10))
    request_url: Mapped[str | None] = mapped_column(Text)
    response_summary: Mapped[str | None] = mapped_column(Text)
    retry_after_seconds: Mapped[float | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    correlation_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # A redelivered broker message cannot create a second row for the same attempt.
        UniqueConstraint("event_id", "cycle", "attempt_number", name="uq_attempts_event_attempt"),
        CheckConstraint(_in("status", ("in_progress", "succeeded", "failed")), name="status_valid"),
        Index("ix_attempts_ws_started", "workspace_id", text("started_at DESC")),
    )


class ReplayOperation(Base):
    """Replay lineage: who replayed which event, why, and under which configuration."""

    __tablename__ = "replay_operations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(Text, server_default="")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    version_mode: Mapped[str] = mapped_column(String(16))
    integration_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_versions.id", ondelete="RESTRICT")
    )
    from_status: Mapped[str] = mapped_column(String(24))
    cycle: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_replay_idempotency"),
        CheckConstraint(_in("version_mode", ("original", "current")), name="version_mode_valid"),
    )


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    report: Mapped[JsonDict | None]
    evidence_refs: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    trace: Mapped[JsonDict | None]
    decisions: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()
    completed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(_in("status", ("completed", "failed")), name="status_valid"),
        CheckConstraint(_in("mode", ("ai", "deterministic")), name="mode_valid"),
        Index(
            "ix_investigations_ws_created", "workspace_id", text("created_at DESC"), text("id DESC")
        ),
    )


class AuditLog(Base):
    """Append-only record of who changed what. ``details`` holds safe metadata only."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(80))
    details: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        Index("ix_audit_ws_created", "workspace_id", text("created_at DESC"), text("id DESC")),
        Index("ix_audit_entity", "workspace_id", "entity_type", "entity_id"),
    )


class RateLimitCounter(Base):
    """Fixed-window counters shared by every instance (see algorithms.md)."""

    __tablename__ = "rate_limit_counters"

    scope_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class LocalQueueMessage(Base):
    """Development-only broker used when RELAY_QUEUE_BACKEND=local. Unused in cloud."""

    __tablename__ = "local_queue_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    topic: Mapped[str] = mapped_column(String(120))
    data: Mapped[JsonDict]
    attributes: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default="ready")
    delivery_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_expires_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint(_in("status", ("ready", "dead")), name="status_valid"),
        Index("ix_local_queue_ready", "available_at", postgresql_where=text("status = 'ready'")),
    )


class DemoDestinationSetting(Base):
    """Failure-injection mode for the built-in demo destination, per workspace."""

    __tablename__ = "demo_destination_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(24), server_default="healthy")
    fail_first_n: Mapped[int] = mapped_column(Integer, server_default=text("2"))
    delay_seconds: Mapped[float] = mapped_column(server_default=text("3"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class DemoDestinationRequest(Base):
    """What the demo destination has seen, keyed by idempotency key."""

    __tablename__ = "demo_destination_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    attempts_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    accepted_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    payload: Mapped[JsonDict | None]
    first_seen_at: Mapped[datetime] = _created()
    accepted_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_demo_requests_key"),
    )
