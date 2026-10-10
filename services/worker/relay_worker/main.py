"""Relay worker: receives broker push messages, delivers events, runs the dispatcher.

Endpoints (all internal, all authenticated):

* ``POST /internal/pubsub/push``        one delivery attempt per message
* ``POST /internal/pubsub/dead-letter`` messages the broker gave up redelivering
* ``POST /internal/outbox/dispatch``    publish due outbox rows, recover expired
                                        leases, run housekeeping

In the cloud, Pub/Sub push subscriptions call the first two and one Cloud
Scheduler job calls the third. Locally, background loops in this process do the
same work so no cloud service is needed.

HTTP status is the acknowledgement: 2xx acks the message, anything else asks
the broker to redeliver.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from relay_core.adapters.registry import build_default_registry
from relay_core.asgi import BodySizeLimitMiddleware, RequestContextMiddleware
from relay_core.config import Settings, get_settings
from relay_core.db import migrate
from relay_core.db.session import SessionFactory, database_is_reachable, get_session_factory, utcnow
from relay_core.errors import RelayError
from relay_core.logging import configure_logging
from relay_core.messaging.base import DeliveryMessage, MalformedMessageError, decode_push_envelope
from relay_core.messaging.local_queue import LocalQueuePump
from relay_core.messaging.publishers import build_publisher
from relay_core.services.maintenance import run_maintenance
from relay_core.services.outbox import OutboxDispatcher
from relay_core.services.processing import Ack, DeliveryProcessor
from relay_core.services.secrets import EnvSecretResolver
from relay_worker.internal_auth import TOKEN_HEADER, InternalAuthenticator

logger = logging.getLogger("relay.worker")

DISPATCH_TIME_BUDGET_SECONDS = 20.0
MAINTENANCE_INTERVAL_SECONDS = 300.0


@dataclass
class WorkerContainer:
    settings: Settings
    session_factory: SessionFactory
    processor: DeliveryProcessor
    dispatcher: OutboxDispatcher
    authenticator: InternalAuthenticator
    clock: Callable[[], datetime]
    pump: LocalQueuePump | None = None


def build_container(
    settings: Settings | None = None, session_factory: SessionFactory | None = None
) -> WorkerContainer:
    settings = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    publisher = build_publisher(settings, session_factory)
    pump = None
    if settings.queue_backend == "local":
        client = httpx.Client(trust_env=False, timeout=60.0)
        token = settings.internal_token.get_secret_value() if settings.internal_token else ""

        def push(path: str, envelope: dict[str, Any]) -> int:
            return client.post(
                f"{settings.worker_base_url}{path}", json=envelope, headers={TOKEN_HEADER: token}
            ).status_code

        pump = LocalQueuePump(
            session_factory, push, topic=settings.pubsub_topic,
            max_delivery_attempts=settings.local_queue_max_delivery_attempts,
        )  # fmt: skip
    return WorkerContainer(
        settings=settings,
        session_factory=session_factory,
        processor=DeliveryProcessor(
            session_factory,
            build_default_registry(settings),
            EnvSecretResolver(),
            lease_margin_seconds=settings.processing_lease_margin_seconds,
        ),
        dispatcher=OutboxDispatcher(
            session_factory,
            publisher,
            lease_seconds=settings.outbox_lease_seconds,
            batch_size=settings.outbox_batch_size,
        ),
        authenticator=InternalAuthenticator(settings),
        clock=utcnow,
        pump=pump,
    )


def run_dispatch_cycle(
    container: WorkerContainer, *, time_budget: float = DISPATCH_TIME_BUDGET_SECONDS
) -> dict[str, int]:
    """One bounded dispatcher pass: publish due messages, then recover expired leases."""
    started = time.monotonic()
    totals = {"claimed": 0, "published": 0, "publish_failed": 0, "leases_recovered": 0}
    while time.monotonic() - started < time_budget:
        stats = container.dispatcher.dispatch_due()
        totals["claimed"] += stats.claimed
        totals["published"] += stats.published
        totals["publish_failed"] += stats.failed
        # Stop when the backlog is drained, or when the broker is refusing
        # messages (retrying immediately would just spin).
        if stats.claimed < container.settings.outbox_batch_size or stats.failed:
            break
    totals["leases_recovered"] = container.processor.reap_expired_leases()
    return totals


class BackgroundLoops:
    """Local stand-in for Cloud Scheduler and the Pub/Sub push subscription."""

    def __init__(self, container: WorkerContainer) -> None:
        self._container = container
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="relay-local-loops", daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("local dispatcher and queue loops started")

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        container = self._container
        interval = container.settings.dispatch_interval_seconds
        last_maintenance = 0.0
        while not self._stop.is_set():
            try:
                run_dispatch_cycle(container, time_budget=5.0)
                if container.pump is not None:
                    container.pump.tick()
                if time.monotonic() - last_maintenance > MAINTENANCE_INTERVAL_SECONDS:
                    last_maintenance = time.monotonic()
                    run_maintenance(
                        container.session_factory,
                        retention_days=container.settings.payload_retention_days,
                        now=container.clock(),
                    )
            except Exception:
                # Keep looping: a transient database error must not end delivery.
                logger.exception("local loop iteration failed")
            self._stop.wait(interval)


def get_container(request: Request) -> WorkerContainer:
    container: WorkerContainer = request.app.state.container
    return container


def require_internal(request: Request, container: WorkerContainer = Depends(get_container)) -> str:
    return container.authenticator.authenticate({k.lower(): v for k, v in request.headers.items()})


def create_app(container: WorkerContainer | None = None) -> FastAPI:
    container = container or build_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loops = None
        if container.settings.background_loops:
            loops = BackgroundLoops(container)
            loops.start()
        yield
        if loops is not None:
            loops.stop()

    app = FastAPI(
        title="Relay Worker",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.container = container

    def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "details": [],
                    "correlation_id": request.scope.get("state", {}).get("correlation_id"),
                }
            },
            status_code=status,
        )

    @app.exception_handler(RelayError)
    async def relay_error(request: Request, exc: RelayError) -> JSONResponse:
        return _error(request, exc.http_status, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # A 500 is a nack: the broker redelivers, bounded by its dead-letter policy.
        logger.exception("unhandled worker error")
        return _error(request, 500, "internal_error", "an unexpected error occurred")

    def _decode(body: Any) -> DeliveryMessage | None:
        try:
            return DeliveryMessage.from_data(decode_push_envelope(body).data)
        except MalformedMessageError as exc:
            # Poison message: redelivery can never fix it, so acknowledge and log.
            logger.error("discarding malformed message", extra={"reason": str(exc)[:200]})
            return None

    @app.post("/internal/pubsub/push")
    def pubsub_push(
        body: dict[str, Any],
        caller: str = Depends(require_internal),
        c: WorkerContainer = Depends(get_container),
    ) -> JSONResponse:
        message = _decode(body)
        if message is None:
            return JSONResponse(
                {"status": "discarded", "detail": "malformed_message"}, status_code=200
            )
        result = c.processor.handle(message)
        # Traffic-driven dispatch: while messages are flowing, due retries are
        # published without waiting for the next scheduler tick. Best effort —
        # the scheduled dispatcher remains the guarantee.
        if result.ack is Ack.ACK and not c.settings.background_loops:
            try:
                c.dispatcher.dispatch_due(limit=10)
            except Exception:
                logger.warning("opportunistic dispatch failed", exc_info=True)
        if result.ack is Ack.NACK:
            return JSONResponse({"status": "retry", "detail": result.detail}, status_code=503)
        return JSONResponse({"status": "ok", "detail": result.detail}, status_code=200)

    @app.post("/internal/pubsub/dead-letter")
    def pubsub_dead_letter(
        body: dict[str, Any],
        caller: str = Depends(require_internal),
        c: WorkerContainer = Depends(get_container),
    ) -> JSONResponse:
        message = _decode(body)
        if message is None:
            return JSONResponse(
                {"status": "discarded", "detail": "malformed_message"}, status_code=200
            )
        result = c.processor.dead_letter_from_queue(message)
        return JSONResponse({"status": "ok", "detail": result.detail}, status_code=200)

    @app.post("/internal/outbox/dispatch")
    def outbox_dispatch(
        caller: str = Depends(require_internal), c: WorkerContainer = Depends(get_container)
    ) -> dict[str, Any]:
        totals = run_dispatch_cycle(c)
        maintenance = run_maintenance(
            c.session_factory, retention_days=c.settings.payload_retention_days, now=c.clock()
        )
        logger.info("dispatch cycle finished", extra=totals)
        return {"status": "ok", **totals, "payloads_purged": maintenance.payloads_purged}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(c: WorkerContainer = Depends(get_container)) -> JSONResponse:
        checks = {"database": "ok" if database_is_reachable(c.session_factory) else "unreachable"}
        if checks["database"] == "ok":
            engine = c.session_factory.kw["bind"]
            current, head = migrate.current_revision(engine), migrate.head_revision()
            checks["migrations"] = (
                "ok" if current == head else f"schema at {current}, expected {head}"
            )
        ready = all(value == "ok" for value in checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    app.add_middleware(BodySizeLimitMiddleware, default_limit=65_536)
    app.add_middleware(RequestContextMiddleware)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn relay_worker.main:app_factory --factory``."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return create_app()
