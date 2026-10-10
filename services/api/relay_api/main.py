"""Relay API: control plane, webhook ingestion and the demo destination.

Run locally with ``uvicorn relay_api.main:app_factory --factory``. Delivery work happens in the
separate worker service (``relay_worker.main:app``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from relay_api.deps import CSRF_HEADER, WORKSPACE_HEADER, Container
from relay_api.routers import auth, config, events, ops, public
from relay_core.adapters.registry import build_default_registry
from relay_core.ai.service import InvestigationService
from relay_core.asgi import CORRELATION_HEADER, BodySizeLimitMiddleware, RequestContextMiddleware
from relay_core.config import Settings, get_settings
from relay_core.db.session import SessionFactory, get_session_factory, utcnow
from relay_core.errors import RelayError
from relay_core.logging import configure_logging
from relay_core.messaging.publishers import build_publisher
from relay_core.services.ingestion import IngestionService
from relay_core.services.outbox import OutboxDispatcher
from relay_core.services.secrets import EnvSecretResolver

logger = logging.getLogger("relay.api")

DESCRIPTION = """\
Relay ingests events from source systems, validates and transforms them with a declarative \
mapping, and delivers them to destination APIs with retries, dead-lettering and audited replay.

**Conventions**

* Control-plane routes live under `/api/v1` and use a session cookie. State-changing requests \
must send the `X-CSRF-Token` returned by `/api/v1/auth/login` or `/api/v1/auth/me`.
* Resources are scoped to the active workspace; send `X-Workspace-Id` to choose one.
* Errors use one envelope: `{"error": {"code", "message", "details", "correlation_id"}}`.
* Lists are keyset-paginated: pass the previous response's `next_cursor` as `?cursor=`.
* Every response carries `X-Correlation-Id`; the same id follows an event through delivery.
"""

Scope = MutableMapping[str, Any]


class SecurityHeadersMiddleware:
    """Conservative response headers. The API serves JSON, never active content."""

    _DOCS = ("/docs", "/redoc", "/openapi.json")

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if not path.startswith(self._DOCS):
                    headers.append(
                        (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
                    )
                if path.startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def build_container(
    settings: Settings | None = None, session_factory: SessionFactory | None = None
) -> Container:
    settings = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    registry = build_default_registry(settings)
    secrets = EnvSecretResolver()
    publisher = build_publisher(settings, session_factory)
    dispatcher = OutboxDispatcher(
        session_factory,
        publisher,
        lease_seconds=settings.outbox_lease_seconds,
        batch_size=settings.outbox_batch_size,
    )
    return Container(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        secrets=secrets,
        publisher=publisher,
        dispatcher=dispatcher,
        ingestion=IngestionService(
            session_factory,
            registry,
            secrets,
            settings,
            dispatcher=dispatcher if settings.inline_publish else None,
        ),
        investigations=InvestigationService(session_factory, settings),
        clock=utcnow,
    )


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
                "correlation_id": request.scope.get("state", {}).get("correlation_id"),
            }
        },
        status_code=status,
        headers=headers,
    )


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()
    settings = container.settings
    app = FastAPI(
        title="Relay API",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=[
            {
                "name": "webhooks",
                "description": "Event ingestion for source systems (HMAC-authenticated).",
            },
            {"name": "auth", "description": "Sessions."},
            {"name": "workspaces", "description": "Workspaces and members."},
            {"name": "connections", "description": "Source and destination endpoints."},
            {
                "name": "integrations",
                "description": "Versioned integration configuration and status.",
            },
            {"name": "events", "description": "Event explorer and replay."},
            {"name": "investigations", "description": "Read-only failure investigation."},
            {"name": "dashboard", "description": "Operational summary."},
            {"name": "audit", "description": "Audit trail."},
            {
                "name": "demo",
                "description": "Demo source simulator and demo destination. Not part of the product path.",
            },
            {"name": "health", "description": "Liveness and readiness."},
        ],
    )
    app.state.container = container

    @app.exception_handler(RelayError)
    async def relay_error(request: Request, exc: RelayError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("request failed", extra={"error_code": exc.code})
        return _error(
            request, exc.http_status, exc.code, exc.message, exc.details, exc.headers or None
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Locations and messages only: the rejected input is never echoed back.
        details = [
            {
                "code": "invalid_request",
                "path": ".".join(
                    str(part)
                    for part in error["loc"]
                    if part not in ("body", "query", "path", "header")
                )
                or str(error["loc"][0]),
                "message": str(error["msg"]).removeprefix("Value error, "),
            }
            for error in exc.errors()[:25]
        ]
        return _error(request, 422, "invalid_request", "the request is not valid", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(
            request, exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail)
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Full detail goes to the log with the correlation id; the client gets
        # nothing that could reveal internals.
        logger.exception("unhandled error")
        return _error(request, 500, "internal_error", "an unexpected error occurred")

    for router in (public.router, auth.router, config.router, events.router, ops.router):
        app.include_router(router)

    # Added last = outermost. Order of execution on a request:
    # context (correlation id) -> body limit -> security headers -> CORS -> routes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Content-Type",
            CSRF_HEADER,
            WORKSPACE_HEADER,
            "Idempotency-Key",
            CORRELATION_HEADER,
        ],
        expose_headers=[CORRELATION_HEADER, "Retry-After"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=settings.max_api_body_bytes,
        limits={
            "/webhooks/": settings.max_webhook_body_bytes,
            "/demo/fulfillment/": settings.max_webhook_body_bytes,
        },
    )
    app.add_middleware(RequestContextMiddleware)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn relay_api.main:app_factory --factory``."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return create_app()
