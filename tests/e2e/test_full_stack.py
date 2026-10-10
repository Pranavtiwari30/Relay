"""End-to-end over real sockets, wired exactly like local development.

Two uvicorn servers (API and worker), the PostgreSQL-backed local queue, the
worker's background dispatcher loop, and the built-in demo destination reached
over real HTTP. Nothing is mocked; time is real, so these are the slow tests.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn

from relay_api.main import build_container as build_api_container
from relay_api.main import create_app as create_api
from relay_core.adapters.registry import build_default_registry
from relay_core.config import Settings
from relay_core.db.session import SessionFactory, session_scope, utcnow
from relay_core.domain.roles import Role
from relay_core.seed import SeedAccount, SeedResult, seed_demo
from relay_core.services.secrets import EnvSecretResolver
from relay_core.simulator import build_request
from relay_worker.main import build_container as build_worker_container
from relay_worker.main import create_app as create_worker
from tests.support import ORIGIN, PASSWORD

WEBHOOK_SECRET = "e2e-webhook-secret-value"
DEMO_TOKEN = "e2e-demo-destination-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerThread:
    def __init__(self, app: Any, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


@dataclass
class Stack:
    api_url: str
    worker_url: str
    seed: SeedResult
    client: httpx.Client
    csrf: str

    def post(self, path: str, body: Any = None, **headers: str) -> httpx.Response:
        return self.client.post(
            path, json=body, headers={"X-CSRF-Token": self.csrf, "Origin": ORIGIN, **headers}
        )

    def put(self, path: str, body: Any) -> httpx.Response:
        return self.client.put(
            path, json=body, headers={"X-CSRF-Token": self.csrf, "Origin": ORIGIN}
        )

    def set_mode(self, mode: str, **extra: Any) -> None:
        response = self.put("/api/v1/demo/destination", {"mode": mode, **extra})
        assert response.status_code == 200, response.text

    def simulate(self, scenario: str) -> list[dict[str, Any]]:
        response = self.post(
            "/api/v1/simulator/send",
            {"connection_id": str(self.seed.source_connection_id), "scenario": scenario},
        )
        assert response.status_code == 200, response.text
        return list(response.json())

    def wait_for(self, event_id: str, statuses: set[str], timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.client.get(f"/api/v1/events/{event_id}").json()
            if last.get("status") in statuses:
                return last
            time.sleep(0.1)
        raise AssertionError(
            f"event {event_id} stuck in {last.get('status')} (wanted {statuses}); last error: {last.get('last_error')}"
        )

    def attempts(self, event_id: str) -> list[dict[str, Any]]:
        return list(self.client.get(f"/api/v1/events/{event_id}/attempts").json())


@pytest.fixture
def stack(
    session_factory: SessionFactory, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Stack]:
    monkeypatch.setenv("RELAY_SECRET_DEMO_WEBHOOK", WEBHOOK_SECRET)
    monkeypatch.setenv("RELAY_SECRET_DEMO_DESTINATION_TOKEN", DEMO_TOKEN)
    api_port, worker_port = free_port(), free_port()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        env="local",
        database_url=database_url,
        queue_backend="local",
        internal_token="e2e-internal-token",
        background_loops=True,
        dispatch_interval_seconds=0.1,
        worker_base_url=f"http://127.0.0.1:{worker_port}",
        demo_destination_base_url=f"http://127.0.0.1:{api_port}",
        cors_allowed_origins=[ORIGIN],
        login_rate_limit_per_15_minutes=1000,
    )
    with session_scope(session_factory) as session:
        seed = seed_demo(
            session,
            accounts=[SeedAccount("admin@demo.test", PASSWORD, Role.ADMIN, "Demo Admin")],
            registry=build_default_registry(settings),
            secrets=EnvSecretResolver(),
            now=utcnow(),
        )
    assert seed.integration_status == "active", seed.notes

    api = ServerThread(create_api(build_api_container(settings, session_factory)), api_port)
    worker = ServerThread(
        create_worker(build_worker_container(settings, session_factory)), worker_port
    )
    api.start()
    worker.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=30)
    try:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@demo.test", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert login.status_code == 200, login.text
        yield Stack(
            f"http://127.0.0.1:{api_port}",
            f"http://127.0.0.1:{worker_port}",
            seed,
            client,
            login.json()["csrf_token"],
        )
    finally:
        client.close()
        worker.stop()
        api.stop()


def test_success_path_from_simulated_source_to_demo_destination(stack: Stack) -> None:
    (sent,) = stack.simulate("valid_order")
    assert sent["http_status"] == 202
    event_id = sent["body"]["results"][0]["event_id"]
    event = stack.wait_for(event_id, {"DELIVERED"})

    # The vendor payload was mapped to the canonical order shape.
    out = event["transformed_payload"]
    assert set(out) == {"order_id", "customer", "lines", "currency", "ordered_at", "source"}
    assert out["order_id"].startswith("A-")
    assert out["currency"] == "USD"
    assert out["customer"]["name"] == out["customer"]["name"].strip()
    assert out["customer"]["email"] == out["customer"]["email"].lower()
    assert all(isinstance(line["quantity"], int) and line["sku"].isupper() for line in out["lines"])
    assert out["ordered_at"].endswith("Z")
    assert event["payload"]["payment"]["card_number"] == "[REDACTED]"
    assert [t["to_status"] for t in event["transitions"]] == [
        "RECEIVED",
        "PENDING_PUBLISH",
        "QUEUED",
        "PROCESSING",
        "DELIVERED",
    ]

    (attempt,) = stack.attempts(event_id)
    assert (attempt["status"], attempt["http_status"]) == ("succeeded", 201)
    assert attempt["request_url"].endswith(f"/demo/fulfillment/{stack.seed.workspace_id}/orders")

    # The demo destination really received that order, keyed by the event id.
    (received,) = stack.client.get("/api/v1/demo/destination/received").json()
    assert (received["idempotency_key"], received["accepted"], received["attempts_seen"]) == (
        event_id,
        True,
        1,
    )
    assert received["payload"]["order_id"] == out["order_id"]

    summary = stack.client.get("/api/v1/dashboard/summary").json()
    assert (
        summary["events"]["received"],
        summary["events"]["delivered"],
        summary["events"]["success_rate"],
    ) == (1, 1, 1.0)
    assert summary["is_demo_workspace"] is True


def test_an_external_sender_can_post_a_signed_webhook(stack: Stack) -> None:
    """No session, no CSRF: exactly what a source system does."""
    built = build_request("valid_order", secret=WEBHOOK_SECRET, now=utcnow(), sequence=7)
    url = f"{stack.api_url}/webhooks/{stack.seed.source_connection_id}"
    with httpx.Client(timeout=30) as sender:
        accepted = sender.post(url, content=built.body, headers=built.headers)
        again = sender.post(url, content=built.body, headers=built.headers)
        forged = build_request("invalid_signature", secret=WEBHOOK_SECRET, now=utcnow(), sequence=8)
        rejected = sender.post(url, content=forged.body, headers=forged.headers)
    assert accepted.status_code == 202, accepted.text
    assert again.json()["results"][0]["outcome"] == "duplicate"
    assert rejected.status_code == 401
    event_id = accepted.json()["results"][0]["event_id"]
    assert stack.wait_for(event_id, {"DELIVERED"})["source_event_id"] == built.event_id
    assert stack.client.get("/api/v1/events").json()["items"].__len__() == 1


def test_simulated_failure_scenarios(stack: Stack) -> None:
    malformed = stack.simulate("malformed")[0]
    assert malformed["http_status"] == 422
    assert {i["path"] for i in malformed["body"]["results"][0]["issues"]} == {
        "order_no",
        "items[0].qty",
    }

    unsigned = stack.simulate("invalid_signature")[0]
    assert (unsigned["http_status"], unsigned["body"]["error"]["code"]) == (
        401,
        "invalid_signature",
    )

    first, second = stack.simulate("duplicate")
    assert [r["body"]["results"][0]["outcome"] for r in (first, second)] == [
        "accepted",
        "duplicate",
    ]

    transform = stack.simulate("transform_error")[0]["body"]["results"][0]["event_id"]
    failed = stack.wait_for(transform, {"VALIDATION_FAILED"})
    assert (failed["failure_stage"], failed["failure_details"][0]["path"]) == (
        "transform",
        "items[0].qty",
    )

    filtered = stack.simulate("filtered")[0]["body"]["results"][0]["event_id"]
    assert stack.wait_for(filtered, {"FILTERED"})["attempt_count"] == 0


def test_flaky_destination_is_retried_until_it_succeeds(stack: Stack) -> None:
    stack.set_mode("flaky", fail_first_n=2)
    event_id = stack.simulate("valid_order")[0]["body"]["results"][0]["event_id"]
    event = stack.wait_for(event_id, {"DELIVERED"})
    assert event["attempt_count"] == 3
    attempts = stack.attempts(event_id)
    assert [(a["status"], a["http_status"]) for a in attempts] == [
        ("failed", 503),
        ("failed", 503),
        ("succeeded", 201),
    ]
    assert len({a["idempotency_key"] for a in attempts}) == 1
    assert [t["to_status"] for t in event["transitions"]].count("RETRY_SCHEDULED") == 2


def test_dead_letter_investigate_and_replay(stack: Stack) -> None:
    stack.set_mode("error_500")
    event_id = stack.simulate("valid_order")[0]["body"]["results"][0]["event_id"]
    dead = stack.wait_for(event_id, {"DEAD_LETTERED"}, timeout=45)
    assert (dead["attempt_count"], dead["max_attempts"], dead["failure_category"]) == (
        4,
        4,
        "http_5xx",
    )
    assert dead["replayable"] is True
    assert (
        stack.client.get("/api/v1/events", params={"status": "DEAD_LETTERED"}).json()["items"][0][
            "id"
        ]
        == event_id
    )

    # No model provider is configured: the diagnosis is rule-based and evidence-linked.
    investigation = stack.post(f"/api/v1/events/{event_id}/investigations").json()
    assert (investigation["mode"], investigation["status"]) == ("deterministic", "completed")
    assert investigation["report"]["likely_cause"]["category"] == "destination_unavailable"
    attempt_refs = {f"attempt:{a['id']}" for a in stack.attempts(event_id)}
    assert attempt_refs & set(investigation["evidence_refs"])

    stack.set_mode("healthy")
    replay = stack.post(
        f"/api/v1/events/{event_id}/replay",
        {"reason": "destination is healthy again"},
        **{"Idempotency-Key": "e2e-replay-00001"},
    )
    assert replay.status_code == 202, replay.text
    recovered = stack.wait_for(event_id, {"DELIVERED"})
    assert recovered["delivery_cycle"] == 2
    attempts = stack.attempts(event_id)
    assert [(a["cycle"], a["status"]) for a in attempts] == [(1, "failed")] * 4 + [(2, "succeeded")]
    assert attempts[-1]["replay_operation_id"] == replay.json()["replay"]["id"]

    actions = [
        a["action"]
        for a in stack.client.get("/api/v1/audit-logs", params={"limit": 50}).json()["items"]
    ]
    for expected in ("event.replay", "investigation.run", "demo.destination_mode"):
        assert expected in actions


def test_lost_response_is_recovered_without_a_duplicate_order(stack: Stack) -> None:
    """The destination stores the order but its reply times out. The retry carries the
    same idempotency key, so the destination recognises it instead of creating a second order."""
    stack.set_mode("slow_success", delay_seconds=3)  # delivery timeout is 2s
    event_id = stack.simulate("valid_order")[0]["body"]["results"][0]["event_id"]
    event = stack.wait_for(event_id, {"DELIVERED"}, timeout=30)
    attempts = stack.attempts(event_id)
    assert [(a["status"], a["failure_category"], a["http_status"]) for a in attempts] == [
        ("failed", "timeout", None),
        ("succeeded", None, 200),
    ]
    assert "duplicate_ignored" in attempts[1]["response_summary"]
    (received,) = stack.client.get("/api/v1/demo/destination/received").json()
    assert (received["attempts_seen"], received["accepted"]) == (2, True)
    assert event["attempt_count"] == 2


def test_worker_is_not_reachable_without_internal_credentials(stack: Stack) -> None:
    with httpx.Client(base_url=stack.worker_url, timeout=10) as outsider:
        for path in (
            "/internal/pubsub/push",
            "/internal/pubsub/dead-letter",
            "/internal/outbox/dispatch",
        ):
            assert outsider.post(path, json={}).status_code == 401
        assert outsider.get("/healthz").status_code == 200
        assert outsider.get("/readyz").json()["status"] == "ready"
