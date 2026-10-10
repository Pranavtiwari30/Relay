"""Adapter contracts.

Core processing depends only on these protocols. Adding a connector means
implementing one protocol and registering it; ingestion, the worker and the API
do not change. ``tests/contract`` runs the same behavioural suite against every
registered adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from relay_core.domain.retry import Disposition, FailureCategory
from relay_core.errors import Issue


@dataclass(frozen=True)
class InboundRequest:
    """A raw inbound request. Header names are lower-cased by the HTTP layer."""

    headers: Mapping[str, str]
    body: bytes
    received_at: datetime


@dataclass(frozen=True)
class SourceIdentity:
    source_event_id: str | None
    event_type: str | None


class SourceAdapter(Protocol):
    adapter_type: str

    def validate_config(self, config: dict[str, Any], *, has_secret: bool) -> list[Issue]: ...

    def authenticate(
        self,
        config: dict[str, Any],
        secret: str | None,
        request: InboundRequest,
        *,
        tolerance_seconds: int,
    ) -> None:
        """Raise ``UnauthenticatedError`` unless the request is authentic."""
        ...

    def identify(
        self, config: dict[str, Any], request: InboundRequest, payload: dict[str, Any]
    ) -> SourceIdentity: ...


@dataclass(frozen=True)
class DeliveryRequest:
    event_id: UUID
    workspace_id: UUID
    payload: dict[str, Any]
    idempotency_key: str
    correlation_id: str
    attempt_number: int
    timeout_seconds: float


@dataclass(frozen=True)
class DeliveryResult:
    """The classified outcome of one delivery attempt. Adapters never raise for these."""

    disposition: Disposition
    duration_ms: int
    failure_category: FailureCategory | None = None
    http_status: int | None = None
    response_summary: str | None = None
    retry_after_seconds: float | None = None
    error_message: str | None = None
    request_method: str | None = None
    request_url: str | None = None


class DestinationAdapter(Protocol):
    adapter_type: str

    def validate_config(self, config: dict[str, Any], *, has_secret: bool) -> list[Issue]: ...

    def deliver(
        self, config: dict[str, Any], secret: str | None, request: DeliveryRequest
    ) -> DeliveryResult:
        """Attempt one delivery and classify the outcome.

        Must enforce ``request.timeout_seconds`` as a hard deadline, must send
        ``request.idempotency_key`` where the destination supports it, and must
        return (not raise) for timeouts, transport errors and HTTP errors.
        """
        ...
