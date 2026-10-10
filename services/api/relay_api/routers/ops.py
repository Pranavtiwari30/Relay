"""Dashboard, audit log, and the demo tooling (simulator and demo destination controls)."""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from relay_api import schemas
from relay_api.deps import Context, require
from relay_api.serializers import audit_out, event_summary, integration_names
from relay_core.adapters.base import InboundRequest
from relay_core.domain.roles import Permission
from relay_core.errors import RelayError, ValidationFailedError
from relay_core.services import connections as connection_service
from relay_core.services import demo as demo_service
from relay_core.services import queries
from relay_core.simulator import SCENARIOS, build_request

router = APIRouter(prefix="/api/v1")


@router.get(
    "/dashboard/summary",
    response_model=schemas.DashboardOut,
    tags=["dashboard"],
    summary="Operational summary computed from stored events",
)
def dashboard(
    context: Context = Depends(require(Permission.EVENT_READ)),
    window: Annotated[str, Query(pattern="^(1h|24h|7d|30d)$")] = "24h",
) -> dict[str, Any]:
    summary = queries.dashboard_summary(
        context.db, context.workspace_id, window=window, now=context.now()
    )
    failures = summary["recent_failures"]
    names = integration_names(context.db, failures)
    summary["recent_failures"] = [event_summary(event, names) for event in failures]
    summary["is_demo_workspace"] = context.workspace.is_demo
    return summary


@router.get(
    "/audit-logs",
    response_model=schemas.Page[schemas.AuditOut],
    tags=["audit"],
    summary="Audit log (newest first)",
)
def audit_logs(
    context: Context = Depends(require(Permission.AUDIT_READ)),
    action: Annotated[
        str | None,
        Query(max_length=80, description="Action prefix, e.g. 'integration.' or 'event.replay'."),
    ] = None,
    entity_type: Annotated[str | None, Query(max_length=40)] = None,
    entity_id: Annotated[str | None, Query(max_length=80)] = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    page = queries.list_audit(
        context.db, context.workspace_id, cursor=cursor, limit=limit,
        action_prefix=action, entity_type=entity_type, entity_id=entity_id,
    )  # fmt: skip
    return {
        "items": [audit_out(row, email) for row, email in page.items],
        "next_cursor": page.next_cursor,
    }


# --- demo tooling ----------------------------------------------------------


def _setting_out(setting: demo_service.DemoSetting) -> dict[str, Any]:
    return {
        "mode": setting.mode,
        "fail_first_n": setting.fail_first_n,
        "delay_seconds": setting.delay_seconds,
        "modes": demo_service.DEMO_MODES,
        "token_configured": bool(os.environ.get(demo_service.DEMO_TOKEN_ENV)),
    }


@router.get(
    "/demo/destination",
    response_model=schemas.DemoSettingOut,
    tags=["demo"],
    summary="Demo destination behaviour",
)
def get_demo_destination(
    context: Context = Depends(require(Permission.EVENT_READ)),
) -> dict[str, Any]:
    return _setting_out(demo_service.get_setting(context.db, context.workspace_id))


@router.put(
    "/demo/destination",
    response_model=schemas.DemoSettingOut,
    tags=["demo"],
    summary="Set the demo destination's failure mode (operators)",
)
def set_demo_destination(
    body: schemas.DemoSettingUpdate, context: Context = Depends(require(Permission.DEMO_USE))
) -> dict[str, Any]:
    setting = demo_service.set_setting(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        mode=body.mode,
        fail_first_n=body.fail_first_n,
        delay_seconds=body.delay_seconds,
    )
    context.db.commit()
    return _setting_out(setting)


@router.get(
    "/demo/destination/received",
    response_model=list[schemas.DemoReceivedOut],
    tags=["demo"],
    summary="What the demo destination has received",
)
def demo_received(
    context: Context = Depends(require(Permission.EVENT_READ)),
) -> list[dict[str, Any]]:
    return [
        {
            "idempotency_key": row.idempotency_key,
            "attempts_seen": row.attempts_seen,
            "accepted": row.accepted_count > 0,
            "payload": row.payload,
            "first_seen_at": row.first_seen_at,
            "accepted_at": row.accepted_at,
        }
        for row in demo_service.list_received(context.db, context.workspace_id)
    ]


@router.get(
    "/simulator/scenarios",
    response_model=schemas.SimulatorCatalog,
    tags=["demo"],
    summary="Built-in source scenarios",
)
def simulator_scenarios(
    context: Context = Depends(require(Permission.EVENT_READ)),
) -> dict[str, Any]:
    return {"scenarios": SCENARIOS}


@router.post(
    "/simulator/send",
    response_model=list[schemas.SimulatorOutcome],
    tags=["demo"],
    summary="Send simulated source events through real ingestion (operators)",
    description=(
        "Builds a correctly signed webhook request for a built-in scenario and submits it through "
        "the same ingestion service as POST /webhooks/{connection_id}. The response reports what "
        "an external sender would have received."
    ),
)
def simulator_send(
    body: schemas.SimulatorSendRequest, context: Context = Depends(require(Permission.DEMO_USE))
) -> list[dict[str, Any]]:
    if body.scenario not in SCENARIOS:
        raise ValidationFailedError(
            f"unknown scenario '{body.scenario}'", details=[{"supported": sorted(SCENARIOS)}]
        )
    container = context.container
    connection = connection_service.get_connection(
        context.db, context.workspace_id, body.connection_id
    )
    if connection.direction != "source":
        raise ValidationFailedError("choose a source connection")
    secret = container.secrets.resolve(connection.secret_ref) if connection.secret_ref else None
    context.db.rollback()

    outcomes: list[dict[str, Any]] = []
    previous: str | None = None
    for index in range(body.count):
        now = context.now()
        # Vary by time so repeated sends are not all the same customer/order.
        sequence = (int(now.timestamp() * 1000) + index) % 100_000
        if body.scenario == "duplicate":
            original = build_request("valid_order", secret=secret, now=now, sequence=sequence)
            requests = [original, original]
        else:
            requests = [
                build_request(
                    body.scenario,
                    secret=secret,
                    now=now,
                    sequence=sequence,
                    previous_event_id=previous,
                )
            ]
        for simulated in requests:
            previous = simulated.event_id
            correlation = f"sim-{simulated.event_id[4:20]}"
            try:
                response = container.ingestion.ingest(
                    connection.id,
                    InboundRequest(headers=simulated.headers, body=simulated.body, received_at=now),
                    correlation_id=correlation,
                )
                status = 422 if response.all_rejected else 202
                payload: dict[str, Any] = {
                    "correlation_id": response.correlation_id,
                    "results": [
                        {
                            "integration_id": str(r.integration_id),
                            "event_id": str(r.event_id),
                            "outcome": r.outcome,
                            "issues": [i.as_dict() for i in r.issues],
                        }
                        for r in response.results
                    ],
                }
            except RelayError as exc:
                status = exc.http_status
                payload = {
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details}
                }
            outcomes.append(
                {
                    "scenario": simulated.scenario if body.scenario != "duplicate" else "duplicate",
                    "source_event_id": simulated.event_id,
                    "http_status": status,
                    "body": json.loads(json.dumps(payload, default=str)),
                }
            )
    return outcomes
