"""Request and response models. These define the published OpenAPI contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

RoleName = Literal["admin", "operator", "viewer"]


class Strict(BaseModel):
    """Request bodies reject unknown fields so typos surface as errors."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] = []
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Pass as ?cursor= for the next page; null on the last page."
    )


class IssueOut(BaseModel):
    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"


# --- auth ------------------------------------------------------------------


class LoginRequest(Strict):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(Strict):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str


class WorkspaceOut(BaseModel):
    id: UUID
    name: str
    slug: str
    is_demo: bool
    role: RoleName


class SessionOut(BaseModel):
    user: UserOut
    workspaces: list[WorkspaceOut]
    active_workspace_id: UUID | None
    role: RoleName | None
    permissions: list[str]
    csrf_token: str = Field(description="Send as X-CSRF-Token on every state-changing request.")
    expires_at: datetime


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    role: RoleName
    is_active: bool
    joined_at: datetime


class MemberCreate(Strict):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    role: RoleName
    password: str | None = Field(default=None, min_length=12, max_length=256)


class MemberUpdate(Strict):
    role: RoleName


# --- connections -----------------------------------------------------------


class ConnectionCreate(Strict):
    name: str = Field(min_length=1, max_length=200)
    direction: Literal["source", "destination"]
    adapter_type: str = Field(min_length=1, max_length=40)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        default=None,
        max_length=200,
        description="e.g. env:RELAY_SECRET_ORDERS_WEBHOOK — a reference, never a value.",
    )


class ConnectionUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    secret_ref: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class ConnectionOut(BaseModel):
    id: UUID
    name: str
    direction: Literal["source", "destination"]
    adapter_type: str
    config: dict[str, Any]
    secret_ref: str | None
    secret_resolvable: bool | None = Field(
        description="Whether the reference resolves in this environment. Null when no secret is referenced."
    )
    is_active: bool
    webhook_path: str | None = Field(
        description="For source connections: the path senders POST to."
    )
    warnings: list[IssueOut] = []
    created_at: datetime
    updated_at: datetime


class AdapterCatalog(BaseModel):
    sources: list[str]
    destinations: list[str]


# --- integrations ----------------------------------------------------------


class IntegrationCreate(Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    source_connection_id: UUID
    destination_connection_id: UUID
    config: dict[str, Any]
    change_note: str = Field(default="", max_length=500)


class IntegrationUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    source_connection_id: UUID | None = None
    destination_connection_id: UUID | None = None
    config: dict[str, Any] | None = None
    change_note: str = Field(default="", max_length=500)


class IntegrationOut(BaseModel):
    id: UUID
    name: str
    description: str
    status: Literal["draft", "active", "paused", "disabled"]
    source_connection_id: UUID
    source_connection_name: str | None
    destination_connection_id: UUID
    destination_connection_name: str | None
    destination_adapter_type: str | None
    current_version_id: UUID | None
    current_version: int | None
    created_at: datetime
    updated_at: datetime


class IntegrationDetail(IntegrationOut):
    config: dict[str, Any]


class VersionOut(BaseModel):
    id: UUID
    version: int
    config: dict[str, Any]
    change_note: str
    created_by: UUID | None
    created_at: datetime
    is_current: bool


class ValidationResult(BaseModel):
    valid: bool = Field(description="True when there are no error-severity issues.")
    issues: list[IssueOut]


class ValidateRequest(Strict):
    config: dict[str, Any] | None = Field(
        default=None, description="Validate this draft instead of the stored current version."
    )


class PreviewRequest(Strict):
    config: dict[str, Any]
    payload: Any


class PreviewResult(BaseModel):
    kind: Literal["ready", "validation_failed", "filtered", "invalid_config"]
    stage: str | None
    issues: list[IssueOut]
    output: Any | None
    filter_reason: str | None = None


class StatusChangeResult(BaseModel):
    integration: IntegrationOut
    held_events_affected: int


# --- events ----------------------------------------------------------------

EventStatusName = Literal[
    "RECEIVED", "HELD", "VALIDATION_FAILED", "FILTERED", "PENDING_PUBLISH", "QUEUED",
    "PROCESSING", "RETRY_SCHEDULED", "DELIVERED", "DEAD_LETTERED", "REPLAY_REQUESTED",
]  # fmt: skip


class EventSummary(BaseModel):
    id: UUID
    integration_id: UUID
    integration_name: str | None
    status: EventStatusName
    event_type: str | None
    source_event_id: str | None
    correlation_id: str
    attempt_count: int
    max_attempts: int
    delivery_cycle: int
    next_attempt_at: datetime | None
    failure_stage: str | None
    failure_category: str | None
    last_error: str | None
    received_at: datetime
    updated_at: datetime
    delivered_at: datetime | None
    dead_lettered_at: datetime | None


class TransitionOut(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    reason: str
    actor_type: str
    actor_user_id: UUID | None
    details: dict[str, Any] | None
    created_at: datetime


class AttemptOut(BaseModel):
    id: UUID
    cycle: int
    attempt_number: int
    status: Literal["in_progress", "succeeded", "failed"]
    disposition: str | None
    failure_category: str | None
    http_status: int | None
    request_method: str | None
    request_url: str | None
    response_summary: str | None = Field(
        description="Redacted and truncated. Untrusted content from the destination."
    )
    retry_after_seconds: float | None
    error_message: str | None
    idempotency_key: str
    integration_version_id: UUID
    replay_operation_id: UUID | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class ReplayOut(BaseModel):
    id: UUID
    event_id: UUID
    cycle: int
    version_mode: Literal["original", "current"]
    integration_version_id: UUID
    from_status: str
    reason: str
    requested_by: UUID | None
    created_at: datetime


class ReplayResponse(BaseModel):
    replay: ReplayOut
    created: bool = Field(
        description="False when this Idempotency-Key had already been used: the original operation is returned."
    )
    event: EventSummary


class ReplayRequest(Strict):
    reason: str = Field(min_length=3, max_length=1000)
    version_mode: Literal["original", "current"] = "current"


class EventDetail(EventSummary):
    integration_version_id: UUID
    integration_version: int | None
    payload: Any = Field(description="Original payload with sensitive fields redacted.")
    transformed_payload: Any | None = Field(
        description="Payload sent to the destination, redacted."
    )
    payload_size_bytes: int
    payload_purged: bool
    failure_details: list[IssueOut] | None
    replayable: bool
    transitions: list[TransitionOut]
    replays: list[ReplayOut]


# --- webhooks --------------------------------------------------------------


class WebhookResult(BaseModel):
    integration_id: UUID
    event_id: UUID
    outcome: Literal["accepted", "duplicate", "validation_failed", "held"]
    issues: list[IssueOut] = []


class WebhookResponse(BaseModel):
    correlation_id: str
    results: list[WebhookResult]


# --- investigations --------------------------------------------------------


class InvestigationOut(BaseModel):
    id: UUID
    event_id: UUID
    status: Literal["completed", "failed"]
    mode: Literal["ai", "deterministic"]
    provider: str | None
    model: str | None
    report: dict[str, Any] | None
    evidence_refs: list[str]
    decisions: dict[str, Any]
    fallback_reason: str | None
    error: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    trace: dict[str, Any] | None
    requested_by: UUID | None
    created_at: datetime
    completed_at: datetime | None


class DecisionRequest(Strict):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


class AiStatus(BaseModel):
    providers: list[dict[str, Any]]
    deterministic_fallback: bool = True


# --- dashboard / audit / demo ----------------------------------------------


class AuditOut(BaseModel):
    id: UUID
    action: str
    entity_type: str
    entity_id: str
    actor_type: str
    actor_user_id: UUID | None
    actor_email: str | None
    details: dict[str, Any]
    correlation_id: str | None
    created_at: datetime


class DashboardEvents(BaseModel):
    received: int
    delivered: int
    failed: int
    in_flight: int
    filtered: int
    held: int
    dead_lettered: int
    validation_failed: int
    by_status: dict[str, int]
    success_rate: float | None = Field(
        description="delivered / (delivered + failed); null when no event has reached an outcome."
    )


class DashboardAttempts(BaseModel):
    total: int
    failed: int
    retries: int
    duration_ms_p50: float | None
    duration_ms_p95: float | None


class DashboardLive(BaseModel):
    retry_scheduled: int
    held: int
    oldest_in_flight_age_seconds: float | None
    oldest_pending_outbox_age_seconds: float | None


class DashboardIntegration(BaseModel):
    id: UUID
    name: str
    status: str
    received: int
    delivered: int
    failed: int
    in_flight: int
    last_event_at: datetime | None
    success_rate: float | None


class SeriesPoint(BaseModel):
    at: datetime
    received: int
    delivered: int
    failed: int


class DashboardSeries(BaseModel):
    bucket: Literal["hour", "day"]
    points: list[SeriesPoint]


class DashboardInvestigations(BaseModel):
    total: int
    ai: int
    fell_back: int
    failed: int
    avg_duration_ms: float | None


class DashboardOut(BaseModel):
    window: Literal["1h", "24h", "7d", "30d"]
    since: datetime
    generated_at: datetime
    is_demo_workspace: bool
    events: DashboardEvents
    attempts: DashboardAttempts
    live: DashboardLive
    recent_failures: list[EventSummary]
    integrations: list[DashboardIntegration]
    series: DashboardSeries
    investigations: DashboardInvestigations


class DemoSettingOut(BaseModel):
    mode: str
    fail_first_n: int
    delay_seconds: float
    modes: dict[str, str]
    token_configured: bool


class DemoSettingUpdate(Strict):
    mode: str
    fail_first_n: int = Field(default=2, ge=1, le=10)
    delay_seconds: float = Field(default=3.0, ge=0, le=15)


class DemoReceivedOut(BaseModel):
    idempotency_key: str
    attempts_seen: int
    accepted: bool
    payload: Any | None
    first_seen_at: datetime
    accepted_at: datetime | None


class SimulatorSendRequest(Strict):
    connection_id: UUID
    scenario: str
    count: int = Field(default=1, ge=1, le=20)


class SimulatorOutcome(BaseModel):
    scenario: str
    source_event_id: str
    http_status: int
    body: dict[str, Any]


class SimulatorCatalog(BaseModel):
    scenarios: dict[str, str]
