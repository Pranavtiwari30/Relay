"""Framework-neutral ASGI middleware shared by the API and worker services."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from relay_core.errors import PayloadTooLargeError
from relay_core.logging import bind_log_context

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = logging.getLogger("relay.http")

CORRELATION_HEADER = "x-correlation-id"
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


class RequestContextMiddleware:
    """Assigns a correlation ID, binds it to the log context, and logs the request.

    A caller-supplied ID is honoured only if it is short and plain, so it can be
    stored and logged safely. The ID is echoed in the response and travels with
    the event through the outbox, the broker message, the worker and the
    outbound delivery request.
    """

    def __init__(self, app: App, *, quiet_paths: tuple[str, ...] = ("/healthz", "/readyz")) -> None:
        self.app = app
        self._quiet = quiet_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = _header(scope, CORRELATION_HEADER.encode())
        correlation = (
            supplied if supplied and _CORRELATION_RE.fullmatch(supplied) else new_correlation_id()
        )
        scope.setdefault("state", {})["correlation_id"] = correlation
        started = time.monotonic()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), correlation.encode()))
                message["headers"] = headers
            await send(message)

        with bind_log_context(correlation_id=correlation):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                path = str(scope.get("path", ""))
                if path not in self._quiet:
                    # Path only: query strings can carry identifiers we do not want in logs.
                    state = scope.get("state", {})
                    extra = {
                        "http_method": scope.get("method"),
                        "http_path": path,
                        "http_status": status,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                    # Set by the application once it has identified the caller.
                    for key in ("workspace_id", "user_id"):
                        if state.get(key):
                            extra[key] = state[key]
                    logger.info("request completed", extra=extra)


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies with 413 before they are buffered.

    Checks ``Content-Length`` up front and also counts bytes as they stream, so
    a chunked upload cannot bypass the limit. ``limits`` maps a path prefix to
    a byte limit; the longest matching prefix wins.
    """

    def __init__(
        self, app: App, *, default_limit: int, limits: dict[str, int] | None = None
    ) -> None:
        self.app = app
        self._default = default_limit
        self._limits = sorted((limits or {}).items(), key=lambda item: -len(item[0]))

    def _limit_for(self, path: str) -> int:
        for prefix, limit in self._limits:
            if path.startswith(prefix):
                return limit
        return self._default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(str(scope.get("path", "")))
        declared = _header(scope, b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await self._reject(scope, send, limit)
            return
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # A RelayError, so a framework's error handler renders the
                    # standard 413 envelope; the except below is the fallback.
                    raise PayloadTooLargeError(f"request body exceeds {limit} bytes")
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except PayloadTooLargeError:
            if not response_started:
                await self._reject(scope, send, limit)

    @staticmethod
    async def _reject(scope: Scope, send: Send, limit: int) -> None:
        correlation = scope.get("state", {}).get("correlation_id")
        body = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"request body exceeds {limit} bytes",
                    "details": [],
                    "correlation_id": correlation,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
