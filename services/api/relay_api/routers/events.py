"""Event explorer, replay, and investigations."""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query

from relay_api import schemas
from relay_api.deps import Context, require
from relay_api.serializers import (
    attempt_out,
    event_detail,
    event_summary,
    integration_names,
    investigation_out,
    replay_out,
)
from relay_core.ai import service as investigation_service
from relay_core.domain.roles import Permission
from relay_core.errors import ValidationFailedError
from relay_core.services import queries
from relay_core.services.replay import request_replay

router = APIRouter(prefix="/api/v1")


@router.get(
    "/events",
    response_model=schemas.Page[schemas.EventSummary],
    tags=["events"],
    summary="List events (newest first, keyset-paginated)",
)
def list_events(
    context: Context = Depends(require(Permission.EVENT_READ)),
    status: Annotated[
        list[schemas.EventStatusName] | None,
        Query(description="Repeat to match any of several statuses."),
    ] = None,
    integration_id: uuid.UUID | None = None,
    received_after: Annotated[
        datetime | None, Query(description="Inclusive lower bound (ISO-8601 with offset).")
    ] = None,
    received_before: Annotated[
        datetime | None, Query(description="Exclusive upper bound (ISO-8601 with offset).")
    ] = None,
    q: Annotated[
        str | None,
        Query(max_length=255, description="Exact event id, source event id, or correlation id."),
    ] = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    for name, value in (("received_after", received_after), ("received_before", received_before)):
        if value is not None and value.tzinfo is None:
            raise ValidationFailedError(
                f"{name} must include a UTC offset, for example 2026-03-01T00:00:00Z"
            )
    page = queries.list_events(
        context.db,
        context.workspace_id,
        queries.EventFilters(
            statuses=list(status or []),
            integration_id=integration_id,
            received_after=received_after,
            received_before=received_before,
            search=q,
        ),
        cursor=cursor,
        limit=limit,
    )
    names = integration_names(context.db, page.items)
    return {
        "items": [event_summary(event, names) for event in page.items],
        "next_cursor": page.next_cursor,
    }


@router.get(
    "/events/{event_id}",
    response_model=schemas.EventDetail,
    tags=["events"],
    summary="Event detail with redacted payloads and state history",
)
def get_event(
    event_id: uuid.UUID, context: Context = Depends(require(Permission.EVENT_READ))
) -> dict[str, Any]:
    event = queries.get_event(context.db, context.workspace_id, event_id)
    return event_detail(
        context.db,
        event,
        queries.list_transitions(context.db, context.workspace_id, event_id),
        queries.list_replays(context.db, context.workspace_id, event_id),
    )


@router.get(
    "/events/{event_id}/attempts",
    response_model=list[schemas.AttemptOut],
    tags=["events"],
    summary="Delivery attempts for an event, oldest first",
)
def list_attempts(
    event_id: uuid.UUID, context: Context = Depends(require(Permission.EVENT_READ))
) -> list[dict[str, Any]]:
    queries.get_event(context.db, context.workspace_id, event_id)
    return [
        attempt_out(a) for a in queries.list_attempts(context.db, context.workspace_id, event_id)
    ]


@router.post(
    "/events/{event_id}/replay",
    response_model=schemas.ReplayResponse,
    status_code=202,
    tags=["events"],
    summary="Replay a dead-lettered or validation-failed event (operators)",
    description=(
        "Opens a new delivery cycle on the same event. Existing attempts and history are kept. "
        "Requires an `Idempotency-Key` header: repeating a request with the same key returns the "
        "original replay operation instead of replaying again."
    ),
)
def replay_event(
    event_id: uuid.UUID,
    body: schemas.ReplayRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    context: Context = Depends(require(Permission.EVENT_REPLAY)),
) -> dict[str, Any]:
    if not idempotency_key:
        raise ValidationFailedError(
            "Idempotency-Key header is required", code="idempotency_key_required"
        )
    operation, created = request_replay(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        event_id=event_id,
        idempotency_key=idempotency_key,
        reason=body.reason,
        version_mode=body.version_mode,
        now=context.now(),
    )
    context.db.commit()
    if created:
        # Fast path; the scheduled dispatcher publishes it regardless.
        with contextlib.suppress(Exception):
            context.container.dispatcher.dispatch_due(limit=10)
    event = queries.get_event(context.db, context.workspace_id, event_id)
    context.db.refresh(event)
    return {
        "replay": replay_out(operation),
        "created": created,
        "event": event_summary(event, integration_names(context.db, [event])),
    }


# --- investigations --------------------------------------------------------


@router.get(
    "/ai/status",
    response_model=schemas.AiStatus,
    tags=["investigations"],
    summary="Which model provider slots are configured",
)
def ai_status(context: Context = Depends(require(Permission.INVESTIGATION_READ))) -> dict[str, Any]:
    return {
        "providers": investigation_service.provider_status(context.container.settings),
        "deterministic_fallback": True,
    }


@router.post(
    "/events/{event_id}/investigations",
    response_model=schemas.InvestigationOut,
    status_code=201,
    tags=["investigations"],
    summary="Investigate an event (operators)",
    description=(
        "Runs synchronously and is bounded in steps and time. Uses a configured model provider "
        "if one is available, otherwise (or on any provider failure) a deterministic rule-based "
        "diagnosis. The investigation reads stored evidence only and changes nothing."
    ),
)
def create_investigation(
    event_id: uuid.UUID, context: Context = Depends(require(Permission.INVESTIGATION_RUN))
) -> dict[str, Any]:
    # Release this request's transaction before the (possibly slow) model call.
    context.db.rollback()
    investigation = context.container.investigations.run(
        workspace_id=context.workspace_id, event_id=event_id, actor=context.actor
    )
    return investigation_out(investigation)


@router.get(
    "/investigations",
    response_model=schemas.Page[schemas.InvestigationOut],
    tags=["investigations"],
    summary="List investigations",
)
def list_investigations(
    context: Context = Depends(require(Permission.INVESTIGATION_READ)),
    event_id: uuid.UUID | None = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    page = investigation_service.list_investigations(
        context.db, context.workspace_id, event_id=event_id, cursor=cursor, limit=limit
    )
    return {
        "items": [investigation_out(row) for row in page.items],
        "next_cursor": page.next_cursor,
    }


@router.get(
    "/investigations/{investigation_id}",
    response_model=schemas.InvestigationOut,
    tags=["investigations"],
    summary="Get an investigation",
)
def get_investigation(
    investigation_id: uuid.UUID, context: Context = Depends(require(Permission.INVESTIGATION_READ))
) -> dict[str, Any]:
    return investigation_out(
        investigation_service.get_investigation(context.db, context.workspace_id, investigation_id)
    )


@router.post(
    "/investigations/{investigation_id}/recommendations/{recommendation_id}/decision",
    response_model=schemas.InvestigationOut,
    tags=["investigations"],
    summary="Record a human decision on a recommendation (operators)",
    description="Records and audits the decision only. It never executes the recommended action.",
)
def decide(
    investigation_id: uuid.UUID,
    recommendation_id: str,
    body: schemas.DecisionRequest,
    context: Context = Depends(require(Permission.RECOMMENDATION_DECIDE)),
) -> dict[str, Any]:
    investigation = investigation_service.record_decision(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        investigation_id=investigation_id,
        recommendation_id=recommendation_id[:32],
        decision=body.decision,
        note=body.note,
        now=context.now(),
    )
    context.db.commit()
    return investigation_out(investigation)
