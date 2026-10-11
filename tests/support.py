"""Shared test harness: a controllable clock, a real local HTTP destination, and helpers
that drive the API and worker through their HTTP interfaces."""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from relay_api.deps import Container
from relay_api.main import create_app as create_api
from relay_core.adapters.http_destination import HttpDestinationAdapter
from relay_core.adapters.registry import AdapterRegistry
from relay_core.adapters.webhook_source import WebhookSourceAdapter
from relay_core.ai.providers.base import ChatProvider
from relay_core.ai.service import InvestigationService
from relay_core.config import Settings
from relay_core.db.models import (
    AuditLog,
    Connection,
    DeliveryAttempt,
    Event,
    EventTransition,
    Integration,
    OutboxMessage,
    User,
    Workspace,
    WorkspaceMembership,
)
from relay_core.db.session import SessionFactory
from relay_core.domain.signatures import compute_signature, format_signature
from relay_core.domain.urlsafety import UrlPolicy
from relay_core.messaging.base import encode_push_envelope
from relay_core.messaging.publishers import MemoryPublisher
from relay_core.services import auth as auth_service
from relay_core.services.event_state import SYSTEM
from relay_core.services.ingestion import IngestionService
from relay_core.services.integrations import change_status, create_integration
from relay_core.services.outbox import OutboxDispatcher
from relay_core.services.processing import DeliveryProcessor
from relay_core.services.secrets import EnvSecretResolver
from relay_worker.internal_auth import TOKEN_HEADER, InternalAuthenticator
from relay_worker.main import WorkerContainer
from relay_worker.main import create_app as create_worker

PASSWORD = "correct horse battery staple"
INTERNAL_TOKEN = "test-internal-token"
WEBHOOK_SECRET = "test-webhook-signing-secret"
DEST_TOKEN = "dest_test_token_abcdef"
ORIGIN = "http://localhost:5173"

_password_hash: str | None = None


def password_hash() -> str:
    """Argon2 is deliberately slow; hash the shared test password once."""
    global _password_hash
    if _password_hash is None:
        _password_hash = auth_service.hash_password(PASSWORD)
    return _password_hash


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


@dataclass
class Scripted:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    raw: bytes | None = None  # write these bytes verbatim, then close


class StubDestination:
    """A real HTTP server on loopback whose responses are scripted per request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.script: deque[Scripted] = deque()
        self.default = Scripted(200, {"ok": True})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw) if raw else None
                except ValueError:
                    parsed = None
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "json": parsed,
                    }
                )
                reply = outer.script.popleft() if outer.script else outer.default
                if reply.delay:
                    time.sleep(reply.delay)
                if reply.raw is not None:
                    self.wfile.write(reply.raw)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                payload = (
                    b""
                    if reply.body is None
                    else (
                        reply.body
                        if isinstance(reply.body, bytes)
                        else json.dumps(reply.body).encode()
                    )
                )
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(payload)

            do_POST = do_PUT = do_PATCH = do_GET = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.host}/orders"

    def enqueue(self, status: int = 200, body: Any = None, **kwargs: Any) -> None:
        self.script.append(
            Scripted(status, body if body is not None else {"status": status}, **kwargs)
        )

    def reset(self) -> None:
        self.requests.clear()
        self.script.clear()
        self.default = Scripted(200, {"ok": True})

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


ORDER_CONFIG: dict[str, Any] = {
    "event_types": [],
    "input_schema": {
        "type": "object",
        "required": ["order_no", "qty"],
        "properties": {
            "order_no": {"type": "string"},
            "qty": {"type": ["string", "integer"]},
            "status": {"type": "string"},
        },
    },
    "filters": [{"path": "status", "op": "ne", "value": "draft"}],
    "mappings": [
        {"target": "order_id", "source": "order_no", "required": True},
        {"target": "quantity", "source": "qty", "required": True, "convert": "integer"},
        {"target": "customer.name", "source": "customer.name", "convert": "trim"},
        {"target": "source", "const": "test"},
    ],
    "output_schema": {
        "type": "object",
        "required": ["order_id", "quantity"],
        "properties": {
            "order_id": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1},
        },
    },
    "retry": {"max_attempts": 3, "base_delay_seconds": 2, "max_delay_seconds": 60},
    "delivery": {"timeout_seconds": 1},
}


def order(order_no: str = "A-1", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_no": order_no,
        "qty": "2",
        "status": "paid",
        "customer": {"name": " Ada ", "email": "ada@example.com"},
        "payment": {"card_number": "4111 1111 1111 1111"},
    }
    payload.update(overrides)
    return payload


@dataclass
class Seeded:
    workspace_id: uuid.UUID
    admin: str
    operator: str
    viewer: str
    source_id: uuid.UUID
    destination_id: uuid.UUID
    integration_id: uuid.UUID


class UserClient:
    """A signed-in API client that sends the CSRF token and Origin like the real UI."""

    def __init__(
        self, client: TestClient, csrf: str, workspace_id: uuid.UUID | None = None
    ) -> None:
        self.http = client
        self.csrf = csrf
        self.workspace_id = workspace_id

    def _headers(self, extra: dict[str, str] | None, unsafe: bool) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.workspace_id is not None:
            headers["X-Workspace-Id"] = str(self.workspace_id)
        if unsafe:
            headers.update({"X-CSRF-Token": self.csrf, "Origin": ORIGIN})
        headers.update(extra or {})
        return headers

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(
            path, headers=self._headers(kwargs.pop("headers", None), False), **kwargs
        )

    def post(self, path: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        return self.http.post(
            path, json=json, headers=self._headers(kwargs.pop("headers", None), True), **kwargs
        )

    def patch(self, path: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        return self.http.patch(
            path, json=json, headers=self._headers(kwargs.pop("headers", None), True), **kwargs
        )

    def put(self, path: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        return self.http.put(
            path, json=json, headers=self._headers(kwargs.pop("headers", None), True), **kwargs
        )

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.delete(
            path, headers=self._headers(kwargs.pop("headers", None), True), **kwargs
        )


class Harness:
    def __init__(
        self,
        session_factory: SessionFactory,
        database_url: str,
        destination: StubDestination,
        *,
        providers: list[ChatProvider] | None = None,
        **setting_overrides: Any,
    ) -> None:
        self.session_factory = session_factory
        self.destination = destination
        self.clock = FakeClock()
        values: dict[str, Any] = {
            "env": "test",
            "database_url": database_url,
            "queue_backend": "memory",
            "internal_token": INTERNAL_TOKEN,
            "cors_allowed_origins": [ORIGIN],
            "destination_allow_private_hosts": [destination.host],
            "webhook_rate_limit_per_minute": 10_000,
            "login_rate_limit_per_15_minutes": 1000,
            "ai_total_timeout_seconds": 5,
            "ai_request_timeout_seconds": 2,
        }
        values.update(setting_overrides)
        self.settings = Settings(_env_file=None, **values)
        self.env: dict[str, str] = {
            "RELAY_SECRET_TEST_WEBHOOK": WEBHOOK_SECRET,
            "RELAY_SECRET_TEST_DEST": DEST_TOKEN,
        }
        self.secrets = EnvSecretResolver(self.env)
        self.registry = AdapterRegistry()
        self.registry.register_source(WebhookSourceAdapter())
        self.http_adapter = HttpDestinationAdapter(
            UrlPolicy(allow_private_hosts=frozenset(self.settings.destination_allow_private_hosts)),
            max_response_bytes=self.settings.destination_max_response_bytes,
        )
        self.registry.register_destination(self.http_adapter)
        self.publisher = MemoryPublisher()
        self.dispatcher = OutboxDispatcher(
            session_factory, self.publisher, lease_seconds=self.settings.outbox_lease_seconds,
            batch_size=self.settings.outbox_batch_size, clock=self.clock,
        )  # fmt: skip
        self.processor = DeliveryProcessor(
            session_factory, self.registry, self.secrets,
            lease_margin_seconds=self.settings.processing_lease_margin_seconds, clock=self.clock,
        )  # fmt: skip
        self.ingestion = IngestionService(
            session_factory, self.registry, self.secrets, self.settings,
            dispatcher=self.dispatcher if self.settings.inline_publish else None, clock=self.clock,
        )  # fmt: skip
        self.investigations = InvestigationService(
            session_factory, self.settings, providers=providers or [], clock=self.clock
        )
        self.api_app = create_api(
            Container(
                settings=self.settings, session_factory=session_factory, registry=self.registry,
                secrets=self.secrets, publisher=self.publisher, dispatcher=self.dispatcher,
                ingestion=self.ingestion, investigations=self.investigations, clock=self.clock,
            )
        )  # fmt: skip
        self.worker_app = create_worker(
            WorkerContainer(
                settings=self.settings, session_factory=session_factory, processor=self.processor,
                dispatcher=self.dispatcher, authenticator=InternalAuthenticator(self.settings), clock=self.clock,
            )
        )  # fmt: skip
        self.api = TestClient(self.api_app)
        self.worker = TestClient(self.worker_app)
        self.seeded: Seeded | None = None

    # -- seeding ------------------------------------------------------------

    def add_user(self, email: str, role: str, workspace_id: uuid.UUID) -> None:
        with self.session_factory.begin() as session:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                user = User(
                    id=uuid.uuid4(),
                    email=email,
                    display_name=email.split("@")[0],
                    password_hash=password_hash(),
                )
                session.add(user)
                session.flush()
            session.add(
                WorkspaceMembership(
                    id=uuid.uuid4(), workspace_id=workspace_id, user_id=user.id, role=role
                )
            )

    def seed(
        self, *, slug: str = "acme", config: dict[str, Any] | None = None, activate: bool = True
    ) -> Seeded:
        with self.session_factory.begin() as session:
            workspace = Workspace(id=uuid.uuid4(), name=f"{slug} workspace", slug=slug)
            session.add(workspace)
            session.flush()
            source = Connection(
                id=uuid.uuid4(), workspace_id=workspace.id, name="Test webhook", direction="source",
                adapter_type="webhook", config={"event_id_path": "event_id"}, secret_ref="env:RELAY_SECRET_TEST_WEBHOOK",
            )  # fmt: skip
            destination = Connection(
                id=uuid.uuid4(), workspace_id=workspace.id, name="Test destination", direction="destination",
                adapter_type="http", config={"url": self.destination.url, "auth": {"type": "bearer"}},
                secret_ref="env:RELAY_SECRET_TEST_DEST",
            )  # fmt: skip
            session.add_all([source, destination])
            session.flush()
            integration = create_integration(
                session, actor=SYSTEM, workspace_id=workspace.id, name="Orders", description="",
                source_connection_id=source.id, destination_connection_id=destination.id,
                config=config or ORDER_CONFIG,
            )  # fmt: skip
            if activate:
                change_status(
                    session, actor=SYSTEM, workspace_id=workspace.id, integration_id=integration.id,
                    action="activate", registry=self.registry, secrets=self.secrets, now=self.clock(),
                )  # fmt: skip
            seeded = Seeded(
                workspace.id, f"admin@{slug}.test", f"operator@{slug}.test", f"viewer@{slug}.test",
                source.id, destination.id, integration.id,
            )  # fmt: skip
        for email, role in (
            (seeded.admin, "admin"),
            (seeded.operator, "operator"),
            (seeded.viewer, "viewer"),
        ):
            self.add_user(email, role, seeded.workspace_id)
        if self.seeded is None:
            self.seeded = seeded
        return seeded

    # -- acting as a user ---------------------------------------------------

    def login(
        self, email: str, password: str = PASSWORD, workspace_id: uuid.UUID | None = None
    ) -> UserClient:
        client = TestClient(self.api_app)
        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 200, response.text
        return UserClient(client, response.json()["csrf_token"], workspace_id)

    # -- acting as a source system ------------------------------------------

    def send_webhook(
        self,
        payload: Any = None,
        *,
        connection_id: uuid.UUID | None = None,
        event_id: str | None = None,
        secret: str | None = WEBHOOK_SECRET,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timestamp: int | None = None,
    ) -> httpx.Response:
        assert self.seeded is not None
        body_payload = order() if payload is None else payload
        if event_id is not None and isinstance(body_payload, dict):
            body_payload = {**body_payload, "event_id": event_id}
        body = raw_body if raw_body is not None else json.dumps(body_payload).encode()
        stamp = str(timestamp if timestamp is not None else int(self.clock().timestamp()))
        request_headers = {"Content-Type": "application/json", "X-Relay-Timestamp": stamp}
        if secret is not None:
            request_headers["X-Relay-Signature"] = format_signature(
                compute_signature(secret.encode(), body, stamp)
            )
        request_headers.update(headers or {})
        return self.api.post(
            f"/webhooks/{connection_id or self.seeded.source_id}",
            content=body,
            headers=request_headers,
        )

    # -- driving the pipeline -----------------------------------------------

    def push(
        self,
        data: dict[str, Any],
        *,
        path: str = "/internal/pubsub/push",
        token: str | None = INTERNAL_TOKEN,
    ) -> httpx.Response:
        envelope = encode_push_envelope(
            data, {}, message_id=uuid.uuid4().hex, subscription="test/sub", delivery_attempt=1
        )
        return self.worker.post(path, json=envelope, headers={TOKEN_HEADER: token} if token else {})

    def deliver_published(self) -> list[httpx.Response]:
        """Push every message the broker currently holds to the worker."""
        responses = []
        while self.publisher.messages:
            data, _ = self.publisher.messages.pop(0)
            responses.append(self.push(data))
        return responses

    def pump(self, rounds: int = 10) -> None:
        """Run dispatcher and worker until nothing is due *at the current clock time*."""
        for _ in range(rounds):
            stats = self.dispatcher.dispatch_due()
            delivered = self.deliver_published()
            if not stats.claimed and not delivered:
                return

    def run_to_completion(self, event_id: str | uuid.UUID, *, max_cycles: int = 12) -> Event:
        """Advance the clock through scheduled retries until the event stops moving."""
        for _ in range(max_cycles):
            self.pump()
            event = self.event(event_id)
            if event.status != "RETRY_SCHEDULED" or event.next_attempt_at is None:
                return event
            self.clock.advance(
                max(0.0, (event.next_attempt_at - self.clock()).total_seconds()) + 0.5
            )
        return self.event(event_id)

    # -- inspecting state ---------------------------------------------------

    def event(self, event_id: str | uuid.UUID) -> Event:
        with self.session_factory() as session:
            return session.get_one(Event, uuid.UUID(str(event_id)))

    def events(self) -> list[Event]:
        with self.session_factory() as session:
            return list(
                session.execute(select(Event).order_by(Event.received_at, Event.id)).scalars()
            )

    def attempts(self, event_id: str | uuid.UUID) -> list[DeliveryAttempt]:
        with self.session_factory() as session:
            return list(
                session.execute(
                    select(DeliveryAttempt)
                    .where(DeliveryAttempt.event_id == uuid.UUID(str(event_id)))
                    .order_by(DeliveryAttempt.cycle, DeliveryAttempt.attempt_number)
                ).scalars()
            )

    def history(self, event_id: str | uuid.UUID) -> list[str]:
        with self.session_factory() as session:
            return [
                row.to_status
                for row in session.execute(
                    select(EventTransition)
                    .where(EventTransition.event_id == uuid.UUID(str(event_id)))
                    .order_by(EventTransition.id)
                ).scalars()
            ]

    def outbox(self, event_id: str | uuid.UUID | None = None) -> list[OutboxMessage]:
        with self.session_factory() as session:
            query = select(OutboxMessage).order_by(
                OutboxMessage.created_at, OutboxMessage.attempt_number
            )
            if event_id is not None:
                query = query.where(OutboxMessage.event_id == uuid.UUID(str(event_id)))
            return list(session.execute(query).scalars())

    def audit(self, action_prefix: str = "") -> list[AuditLog]:
        with self.session_factory() as session:
            rows = session.execute(
                select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)
            ).scalars()
            return [row for row in rows if row.action.startswith(action_prefix)]

    def integration(self, integration_id: uuid.UUID | None = None) -> Integration:
        assert self.seeded is not None
        with self.session_factory() as session:
            return session.get_one(Integration, integration_id or self.seeded.integration_id)

    def accepted_event_id(self, response: httpx.Response) -> str:
        assert response.status_code == 202, response.text
        return str(response.json()["results"][0]["event_id"])
