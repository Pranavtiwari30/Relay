"""Built-in demo destination: a small "fulfilment API" with switchable failure modes.

This is demonstration tooling, clearly separated from the product path. It lets
a reviewer reproduce success, retry, dead-letter and replay without any external
system. It honours idempotency keys so duplicate deliveries are observable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from relay_core.db.models import DemoDestinationRequest, DemoDestinationSetting
from relay_core.db.session import SessionFactory, session_scope
from relay_core.errors import ValidationFailedError
from relay_core.services.audit import record_audit
from relay_core.services.event_state import Actor

DEMO_TOKEN_ENV: Final = "RELAY_SECRET_DEMO_DESTINATION_TOKEN"  # noqa: S105 - env var name, not a secret
DEMO_TOKEN_REF: Final = f"env:{DEMO_TOKEN_ENV}"
MAX_DELAY_SECONDS: Final = 15.0

DEMO_MODES: Final[dict[str, str]] = {
    "healthy": "Accepts every order (HTTP 201).",
    "flaky": "Fails the first N attempts for each order with HTTP 503, then accepts.",
    "error_500": "Always fails with HTTP 503 (retryable until attempts are exhausted).",
    "rate_limited": "Always answers HTTP 429 with a Retry-After header.",
    "timeout": "Waits longer than the delivery timeout, then answers 504. The order is not stored.",
    "slow_success": "Stores the order, then stalls on first sight so the response is lost. "
    "The retry is recognised by its idempotency key.",
    "reject_400": "Always rejects with HTTP 422 (permanent: no retry).",
    "unauthorized": "Always answers HTTP 401, as if the credential were wrong (permanent).",
}


@dataclass(frozen=True)
class DemoSetting:
    mode: str = "healthy"
    fail_first_n: int = 2
    delay_seconds: float = 3.0


@dataclass(frozen=True)
class DemoResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    sleep_seconds: float = 0.0


def get_setting(session: Session, workspace_id: uuid.UUID) -> DemoSetting:
    row = session.get(DemoDestinationSetting, workspace_id)
    if row is None:
        return DemoSetting()
    return DemoSetting(row.mode, row.fail_first_n, row.delay_seconds)


def set_setting(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    mode: str,
    fail_first_n: int,
    delay_seconds: float,
) -> DemoSetting:
    if mode not in DEMO_MODES:
        raise ValidationFailedError(
            f"unknown demo mode '{mode}'", details=[{"supported": sorted(DEMO_MODES)}]
        )
    if not 1 <= fail_first_n <= 10 or not 0 <= delay_seconds <= MAX_DELAY_SECONDS:
        raise ValidationFailedError(
            f"fail_first_n must be 1-10 and delay_seconds 0-{MAX_DELAY_SECONDS:g}"
        )
    session.execute(
        pg_insert(DemoDestinationSetting)
        .values(
            workspace_id=workspace_id,
            mode=mode,
            fail_first_n=fail_first_n,
            delay_seconds=delay_seconds,
            updated_by=actor.user_id,
        )
        .on_conflict_do_update(
            index_elements=[DemoDestinationSetting.workspace_id],
            set_={
                "mode": mode,
                "fail_first_n": fail_first_n,
                "delay_seconds": delay_seconds,
                "updated_by": actor.user_id,
                "updated_at": func.now(),
            },
        )
    )
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="demo.destination_mode",
        entity_type="demo_destination",
        entity_id=workspace_id,
        details={"mode": mode, "fail_first_n": fail_first_n, "delay_seconds": delay_seconds},
    )
    return DemoSetting(mode, fail_first_n, delay_seconds)


def _validate_order(payload: Any) -> dict[str, str]:
    """The demo API's own contract for a canonical order."""
    problems: dict[str, str] = {}
    if not isinstance(payload, dict):
        return {"$": "body must be a JSON object"}
    if not isinstance(payload.get("order_id"), str) or not payload["order_id"]:
        problems["order_id"] = "is required and must be a non-empty string"
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        problems["lines"] = "is required and must be a non-empty array"
    else:
        for index, line in enumerate(lines[:100]):
            if not isinstance(line, dict) or not isinstance(line.get("sku"), str):
                problems[f"lines[{index}].sku"] = "is required and must be a string"
            quantity = line.get("quantity") if isinstance(line, dict) else None
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                problems[f"lines[{index}].quantity"] = "must be an integer >= 1"
    return problems


def handle_order(
    session_factory: SessionFactory,
    *,
    workspace_id: uuid.UUID,
    idempotency_key: str,
    payload: Any,
    now: datetime,
) -> DemoResponse:
    key = idempotency_key[:128]
    with session_scope(session_factory) as session:
        setting = get_setting(session, workspace_id)
        seen, accepted = session.execute(
            pg_insert(DemoDestinationRequest)
            .values(
                id=uuid.uuid4(), workspace_id=workspace_id, idempotency_key=key, attempts_seen=1
            )
            .on_conflict_do_update(
                constraint="uq_demo_requests_key",
                set_={"attempts_seen": DemoDestinationRequest.attempts_seen + 1},
            )
            .returning(DemoDestinationRequest.attempts_seen, DemoDestinationRequest.accepted_count)
        ).one()
        reference = f"FUL-{uuid.uuid5(workspace_id, key).hex[:10].upper()}"

        def accept(sleep: float = 0.0) -> DemoResponse:
            if accepted:
                # Same idempotency key as an order already stored: acknowledge
                # without creating a second one.
                return DemoResponse(200, {"status": "duplicate_ignored", "order_ref": reference})
            problems = _validate_order(payload)
            if problems:
                return DemoResponse(422, {"error": "order failed validation", "fields": problems})
            row = session.execute(
                select(DemoDestinationRequest)
                .where(
                    DemoDestinationRequest.workspace_id == workspace_id,
                    DemoDestinationRequest.idempotency_key == key,
                )
                .with_for_update()
            ).scalar_one()
            row.accepted_count = 1
            row.accepted_at = now
            row.payload = payload
            return DemoResponse(
                201, {"status": "accepted", "order_ref": reference}, sleep_seconds=sleep
            )

        mode = setting.mode
        if mode == "error_500":
            return DemoResponse(503, {"error": "fulfilment service temporarily unavailable"})
        if mode == "rate_limited":
            return DemoResponse(429, {"error": "rate limit exceeded"}, headers={"Retry-After": "2"})
        if mode == "timeout":
            return DemoResponse(
                504, {"error": "upstream timeout"}, sleep_seconds=setting.delay_seconds
            )
        if mode == "reject_400":
            return DemoResponse(
                422,
                {
                    "error": "order rejected",
                    "fields": {"lines[0].quantity": "exceeds stock on hand"},
                },
            )
        if mode == "unauthorized":
            return DemoResponse(401, {"error": "invalid API token"})
        if mode == "flaky" and seen <= setting.fail_first_n:
            return DemoResponse(
                503, {"error": "fulfilment service temporarily unavailable", "attempt": seen}
            )
        if mode == "slow_success" and not accepted:
            return accept(sleep=setting.delay_seconds)
        return accept()


def list_received(
    session: Session, workspace_id: uuid.UUID, limit: int = 20
) -> list[DemoDestinationRequest]:
    return list(
        session.execute(
            select(DemoDestinationRequest)
            .where(DemoDestinationRequest.workspace_id == workspace_id)
            .order_by(DemoDestinationRequest.first_seen_at.desc())
            .limit(limit)
        ).scalars()
    )
