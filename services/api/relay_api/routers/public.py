"""Unauthenticated-by-session endpoints: webhooks, health, and the demo destination.

None of these use the browser session. Webhooks authenticate with an HMAC
signature; the demo destination with a bearer token; health checks are open.
"""

from __future__ import annotations

import hmac
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from relay_api import schemas
from relay_api.deps import Container, get_container
from relay_core.adapters.base import InboundRequest
from relay_core.db import migrate
from relay_core.db.session import database_is_reachable
from relay_core.errors import BadRequestError, UnauthenticatedError
from relay_core.services import demo as demo_service
from relay_core.services.ingestion import parse_json_object

router = APIRouter()

ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": schemas.ErrorResponse, "description": "Malformed JSON"},
    401: {"model": schemas.ErrorResponse, "description": "Signature verification failed"},
    404: {"model": schemas.ErrorResponse, "description": "Unknown webhook endpoint"},
    413: {"model": schemas.ErrorResponse, "description": "Body too large"},
    415: {"model": schemas.ErrorResponse, "description": "Content-Type is not JSON"},
    422: {
        "model": schemas.WebhookResponse,
        "description": "Rejected by validation for every matching integration",
    },
    429: {"model": schemas.ErrorResponse, "description": "Rate limit exceeded"},
}


@router.post(
    "/webhooks/{connection_id}",
    response_model=schemas.WebhookResponse,
    status_code=202,
    responses=ERRORS,
    tags=["webhooks"],
    summary="Submit an event",
    description=(
        "Accepts a JSON object. If the connection has a signing secret the request must carry "
        "`X-Relay-Timestamp` (Unix seconds) and `X-Relay-Signature: sha256=<hex>` where the "
        "digest is HMAC-SHA256 over `<timestamp>.<raw body>`. Returns 202 only after the event "
        "is durably stored; delivery is asynchronous. Re-sending the same event id is safe and "
        "reports `duplicate`."
    ),
)
async def receive_webhook(
    connection_id: uuid.UUID, request: Request, container: Container = Depends(get_container)
) -> Response:
    body = await request.body()
    inbound = InboundRequest(
        headers={key.lower(): value for key, value in request.headers.items()},
        body=body,
        received_at=container.clock(),
    )
    correlation_id = request.scope.get("state", {}).get("correlation_id") or uuid.uuid4().hex
    # Ingestion is synchronous database work; keep it off the event loop.
    from starlette.concurrency import run_in_threadpool

    result = await run_in_threadpool(
        container.ingestion.ingest, connection_id, inbound, correlation_id=correlation_id
    )
    payload = schemas.WebhookResponse(
        correlation_id=result.correlation_id,
        results=[
            schemas.WebhookResult(
                integration_id=r.integration_id,
                event_id=r.event_id,
                outcome=r.outcome,
                issues=[schemas.IssueOut.model_validate(issue.as_dict()) for issue in r.issues],
            )
            for r in result.results
        ],
    )
    return JSONResponse(
        payload.model_dump(mode="json"), status_code=422 if result.all_rejected else 202
    )


@router.get("/healthz", tags=["health"], summary="Liveness: the process is running")
def healthz() -> dict[str, str]:
    """No dependencies are checked, so a database outage does not restart healthy instances."""
    return {"status": "ok"}


@router.get("/readyz", tags=["health"], summary="Readiness: database reachable and schema current")
def readyz(container: Container = Depends(get_container)) -> Response:
    checks: dict[str, str] = {}
    if not database_is_reachable(container.session_factory):
        checks["database"] = "unreachable"
    else:
        checks["database"] = "ok"
        engine = container.session_factory.kw["bind"]
        current, head = migrate.current_revision(engine), migrate.head_revision()
        checks["migrations"] = "ok" if current == head else f"schema at {current}, expected {head}"
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )


@router.post(
    "/demo/fulfillment/{workspace_id}/orders",
    tags=["demo"],
    summary="Built-in demo destination (a pretend fulfilment API)",
    description="Target of the `demo` destination adapter. Behaviour is switchable for failure drills; see /api/v1/demo/destination.",
    include_in_schema=True,
)
async def demo_fulfillment(
    workspace_id: uuid.UUID, request: Request, container: Container = Depends(get_container)
) -> Response:
    expected = os.environ.get(demo_service.DEMO_TOKEN_ENV)
    if not expected:
        return JSONResponse(
            {"error": "demo destination is not configured on this server"}, status_code=503
        )
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided.encode(), f"Bearer {expected}".encode()):
        raise UnauthenticatedError("invalid API token")
    try:
        payload = parse_json_object(await request.body())
    except BadRequestError:
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    key = request.headers.get("idempotency-key") or f"no-key-{uuid.uuid4().hex}"
    from starlette.concurrency import run_in_threadpool

    def handle() -> demo_service.DemoResponse:
        outcome = demo_service.handle_order(
            container.session_factory,
            workspace_id=workspace_id,
            idempotency_key=key,
            payload=payload,
            now=container.clock(),
        )
        if outcome.sleep_seconds > 0:
            # Bounded (<= 15s): simulates a slow destination for timeout drills.
            time.sleep(min(outcome.sleep_seconds, demo_service.MAX_DELAY_SECONDS))
        return outcome

    outcome = await run_in_threadpool(handle)
    return JSONResponse(outcome.body, status_code=outcome.status, headers=outcome.headers)
