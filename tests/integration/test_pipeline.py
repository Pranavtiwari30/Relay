"""Webhook -> persist -> queue -> transform -> deliver, and every way that can fail.

These tests run against real PostgreSQL and a real HTTP destination on loopback.
The broker is an in-memory publisher whose messages are pushed to the worker's
real HTTP endpoint in Pub/Sub envelope format.
"""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from sqlalchemy import func, select

from relay_core.db.models import Event, OutboxMessage
from tests.support import DEST_TOKEN, Harness, order

# --- acceptance 2: the happy path ------------------------------------------


def test_event_is_persisted_queued_transformed_and_delivered(harness: Harness) -> None:
    response = harness.send_webhook(order("A-100"), event_id="evt-1")
    assert response.status_code == 202
    body = response.json()
    assert body["results"][0]["outcome"] == "accepted"
    assert response.headers["x-correlation-id"] == body["correlation_id"]
    event_id = body["results"][0]["event_id"]

    # Durable before acknowledgement; inline publish already handed it to the broker.
    assert harness.event(event_id).status == "QUEUED"
    assert harness.destination.requests == []

    harness.pump()
    event = harness.event(event_id)
    assert event.status == "DELIVERED"
    assert event.delivered_at is not None
    assert event.attempt_count == 1
    assert event.transformed_payload == {
        "order_id": "A-100", "quantity": 2, "customer": {"name": "Ada"}, "source": "test",
    }  # fmt: skip
    assert harness.history(event_id) == [
        "RECEIVED",
        "PENDING_PUBLISH",
        "QUEUED",
        "PROCESSING",
        "DELIVERED",
    ]

    (request,) = harness.destination.requests
    assert request["method"] == "POST"
    assert request["json"] == event.transformed_payload
    headers = request["headers"]
    assert headers["authorization"] == f"Bearer {DEST_TOKEN}"
    assert headers["idempotency-key"] == event_id
    assert headers["x-relay-event-id"] == event_id
    assert headers["x-relay-correlation-id"] == body["correlation_id"] == event.correlation_id
    assert headers["x-relay-attempt"] == "1"
    assert headers["content-type"] == "application/json"

    (attempt,) = harness.attempts(event_id)
    assert (attempt.status, attempt.disposition, attempt.http_status) == (
        "succeeded",
        "success",
        200,
    )
    assert attempt.request_url == harness.destination.url
    assert attempt.duration_ms is not None
    assert attempt.integration_version_id == event.integration_version_id


def test_caller_supplied_correlation_id_follows_the_event_end_to_end(harness: Harness) -> None:
    response = harness.send_webhook(
        event_id="evt-c", headers={"X-Correlation-Id": "trace-abc-12345"}
    )
    event_id = harness.accepted_event_id(response)
    harness.pump()
    assert response.headers["x-correlation-id"] == "trace-abc-12345"
    assert harness.event(event_id).correlation_id == "trace-abc-12345"
    assert harness.destination.requests[0]["headers"]["x-relay-correlation-id"] == "trace-abc-12345"
    assert harness.attempts(event_id)[0].correlation_id == "trace-abc-12345"


def test_unsafe_correlation_header_is_replaced(harness: Harness) -> None:
    response = harness.send_webhook(
        event_id="evt-x", headers={"X-Correlation-Id": "bad id\twith spaces"}
    )
    assert response.status_code == 202
    assert response.headers["x-correlation-id"] != "bad id\twith spaces"
    assert len(response.headers["x-correlation-id"]) == 32


# --- acceptance 3: deduplication -------------------------------------------


def test_repeating_an_event_is_deduplicated(harness: Harness) -> None:
    first = harness.send_webhook(order("A-1"), event_id="evt-dup")
    second = harness.send_webhook(order("A-1"), event_id="evt-dup")
    # Same id with a different body is still the same event: identity, not content.
    third = harness.send_webhook(order("A-1", qty="9"), event_id="evt-dup")
    assert [r.status_code for r in (first, second, third)] == [202, 202, 202]
    assert second.json()["results"][0]["outcome"] == "duplicate"
    assert third.json()["results"][0]["outcome"] == "duplicate"
    assert {r.json()["results"][0]["event_id"] for r in (first, second, third)} == {
        harness.accepted_event_id(first)
    }

    harness.pump()
    assert len(harness.events()) == 1
    assert len(harness.destination.requests) == 1
    assert harness.destination.requests[0]["json"]["quantity"] == 2


def test_without_an_event_id_identical_bodies_are_deduplicated(harness: Harness) -> None:
    body = json.dumps({"order_no": "A-7", "qty": 1}).encode()
    first = harness.send_webhook(raw_body=body)
    second = harness.send_webhook(raw_body=body)
    different = harness.send_webhook(raw_body=json.dumps({"order_no": "A-8", "qty": 1}).encode())
    assert first.json()["results"][0]["outcome"] == "accepted"
    assert second.json()["results"][0]["outcome"] == "duplicate"
    assert different.json()["results"][0]["outcome"] == "accepted"
    assert len(harness.events()) == 2


def test_concurrent_duplicates_create_exactly_one_event(harness: Harness) -> None:
    """The unique constraint, not application timing, is the correctness boundary."""
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def submit() -> None:
        barrier.wait()
        response = harness.send_webhook(order("A-RACE"), event_id="evt-race")
        with lock:
            outcomes.append(response.json()["results"][0]["outcome"])

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["accepted"] + ["duplicate"] * 7
    with harness.session_factory() as session:
        assert session.execute(select(func.count()).select_from(Event)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(OutboxMessage)).scalar_one() == 1
    harness.pump()
    assert len(harness.destination.requests) == 1


# --- acceptance 4: malformed input -----------------------------------------


def test_schema_invalid_payload_is_stored_with_actionable_errors_and_never_delivered(
    harness: Harness,
) -> None:
    response = harness.send_webhook({"qty": {"n": 2}, "status": "paid"}, event_id="evt-bad")
    assert response.status_code == 422
    result = response.json()["results"][0]
    assert result["outcome"] == "validation_failed"
    assert {(i["code"], i["path"]) for i in result["issues"]} == {
        ("required_field_missing", "order_no"),
        ("schema_type", "qty"),
    }
    assert all(i["message"] for i in result["issues"])

    harness.pump()
    event = harness.event(result["event_id"])
    assert event.status == "VALIDATION_FAILED"
    assert event.failure_stage == "input_schema"
    assert len(event.failure_details or []) == 2
    assert harness.history(event.id) == ["RECEIVED", "VALIDATION_FAILED"]
    assert harness.outbox(event.id) == []
    assert harness.attempts(event.id) == []
    assert harness.destination.requests == []


def test_payload_that_passes_input_schema_but_cannot_be_transformed_fails_in_the_worker(
    harness: Harness,
) -> None:
    event_id = harness.accepted_event_id(
        harness.send_webhook(order(qty="two dozen"), event_id="evt-t")
    )
    harness.pump()
    event = harness.event(event_id)
    assert (event.status, event.failure_stage) == ("VALIDATION_FAILED", "transform")
    assert event.failure_details == [
        {
            "code": "conversion_failed",
            "path": "qty",
            "message": "'two dozen' is not a base-10 integer (for 'quantity')",
            "severity": "error",
        }
    ]
    assert harness.history(event_id)[-2:] == ["PROCESSING", "VALIDATION_FAILED"]
    assert harness.attempts(event_id) == []
    assert harness.destination.requests == []


def test_output_schema_violation_fails_before_delivery(harness: Harness) -> None:
    event_id = harness.accepted_event_id(harness.send_webhook(order(qty="0"), event_id="evt-o"))
    harness.pump()
    event = harness.event(event_id)
    assert (event.status, event.failure_stage) == ("VALIDATION_FAILED", "output_schema")
    assert event.failure_details
    assert event.failure_details[0]["path"] == "quantity"
    assert harness.destination.requests == []


def test_filtered_event_is_accepted_but_not_delivered(harness: Harness) -> None:
    event_id = harness.accepted_event_id(
        harness.send_webhook(order(status="draft"), event_id="evt-f")
    )
    harness.pump()
    assert harness.event(event_id).status == "FILTERED"
    assert harness.history(event_id)[-1] == "FILTERED"
    assert harness.destination.requests == []


@pytest.mark.parametrize(
    ("raw_body", "headers", "status", "code"),
    [
        (b"{not json", {}, 400, "invalid_json"),
        (b"[1,2,3]", {}, 400, "invalid_json"),
        (b'"just a string"', {}, 400, "invalid_json"),
        (b'{"a": NaN}', {}, 400, "invalid_json"),
        (b'{"a": "\\u0000"}', {}, 400, "invalid_json"),
        (b"\xff\xfe", {}, 400, "invalid_json"),
        (b"[" * 100_000, {}, 400, "invalid_json"),
        (
            b'{"order_no":"A","qty":1}',
            {"Content-Type": "text/plain"},
            415,
            "unsupported_media_type",
        ),
        (
            b'{"order_no":"A","qty":1}',
            {"Content-Type": "application/x-www-form-urlencoded"},
            415,
            "unsupported_media_type",
        ),
    ],
)
def test_malformed_requests_are_rejected_without_persisting(
    harness: Harness, raw_body: bytes, headers: dict[str, str], status: int, code: str
) -> None:
    response = harness.send_webhook(raw_body=raw_body, headers=headers)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == response.headers["x-correlation-id"]
    assert harness.events() == []


def test_vendor_json_content_types_are_accepted(harness: Harness) -> None:
    response = harness.send_webhook(
        event_id="evt-v", headers={"Content-Type": "application/vnd.acme+json; charset=utf-8"}
    )
    assert response.status_code == 202


def test_oversized_body_is_rejected_by_declared_and_by_streamed_length(harness: Harness) -> None:
    limit = harness.settings.max_webhook_body_bytes
    big = json.dumps({"order_no": "A", "qty": 1, "pad": "x" * (limit + 10)}).encode()
    declared = harness.send_webhook(raw_body=big)
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "payload_too_large"

    def chunks():  # type: ignore[no-untyped-def]
        for start in range(0, len(big), 8192):
            yield big[start : start + 8192]

    # No Content-Length: the limit must still hold while the body streams in.
    streamed = harness.api.post(
        f"/webhooks/{harness.seeded.source_id}",
        content=chunks(),
        headers={"Content-Type": "application/json"},  # type: ignore[union-attr]
    )
    assert streamed.status_code == 413
    assert harness.events() == []


def test_unknown_or_non_source_connection_is_404(harness: Harness) -> None:
    assert harness.seeded is not None
    assert harness.send_webhook(connection_id=uuid.uuid4()).status_code == 404
    # A destination connection id must not act as a webhook endpoint.
    response = harness.send_webhook(connection_id=harness.seeded.destination_id)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_endpoint"
    assert harness.api.post("/webhooks/not-a-uuid", content=b"{}").status_code == 422


def test_event_with_no_matching_integration_is_rejected(harness: Harness) -> None:
    client = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    config = {**json.loads(json.dumps(harness_config(harness))), "event_types": ["order.created"]}
    assert (
        client.patch(
            f"/api/v1/integrations/{harness.seeded.integration_id}", {"config": config}
        ).status_code
        == 200
    )  # type: ignore[union-attr]

    wrong = harness.send_webhook(event_id="evt-w", headers={"X-Relay-Event-Type": "refund.created"})
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "no_matching_integration"
    assert harness.events() == []
    right = harness.send_webhook(event_id="evt-r", headers={"X-Relay-Event-Type": "order.created"})
    assert right.status_code == 202
    assert harness.event(harness.accepted_event_id(right)).event_type == "order.created"


def harness_config(harness: Harness) -> dict:  # type: ignore[type-arg]
    from tests.support import ORDER_CONFIG

    return ORDER_CONFIG


# --- webhook authentication -------------------------------------------------


def test_webhook_signature_is_enforced(harness: Harness) -> None:
    unsigned = harness.send_webhook(event_id="e1", secret=None)
    forged = harness.send_webhook(event_id="e2", secret="not-the-secret")
    stale = harness.send_webhook(event_id="e3", timestamp=int(harness.clock().timestamp()) - 3600)
    future = harness.send_webhook(event_id="e4", timestamp=int(harness.clock().timestamp()) + 3600)
    for response in (unsigned, forged, stale, future):
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["code"] == "invalid_signature"
        # One generic message: the caller cannot tell which check failed.
        assert error["message"] == "webhook signature verification failed"
        assert error["details"] == []
    assert harness.events() == []


def test_signature_covers_the_exact_body(harness: Harness) -> None:
    import hashlib
    import hmac

    from tests.support import WEBHOOK_SECRET

    body = json.dumps(order("A-1")).encode()
    stamp = str(int(harness.clock().timestamp()))
    signature = hmac.new(
        WEBHOOK_SECRET.encode(), f"{stamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    tampered = harness.api.post(
        f"/webhooks/{harness.seeded.source_id}",  # type: ignore[union-attr]
        content=body.replace(b'"2"', b'"9"'),
        headers={
            "Content-Type": "application/json",
            "X-Relay-Timestamp": stamp,
            "X-Relay-Signature": f"sha256={signature}",
        },
    )
    assert tampered.status_code == 401
    genuine = harness.api.post(
        f"/webhooks/{harness.seeded.source_id}",  # type: ignore[union-attr]
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Relay-Timestamp": stamp,
            "X-Relay-Signature": f"sha256={signature}",
        },
    )
    assert genuine.status_code == 202


def test_configured_but_unresolvable_signing_secret_fails_closed(harness: Harness) -> None:
    del harness.env["RELAY_SECRET_TEST_WEBHOOK"]
    response = harness.send_webhook(event_id="evt-s")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "secret_unavailable"
    assert harness.events() == []


def test_webhook_rate_limit_is_shared_state_and_counts_only_authentic_requests(
    session_factory,
    database_url,
    destination,  # type: ignore[no-untyped-def]
) -> None:
    harness = Harness(session_factory, database_url, destination, webhook_rate_limit_per_minute=3)
    harness.seed()
    # Forged requests must not spend the legitimate sender's allowance.
    for index in range(5):
        assert harness.send_webhook(event_id=f"forged-{index}", secret="wrong").status_code == 401
    statuses = [harness.send_webhook(event_id=f"evt-{index}").status_code for index in range(5)]
    assert statuses == [202, 202, 202, 429, 429]
    limited = harness.send_webhook(event_id="evt-z")
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    # A second "instance" (fresh service objects, same database) sees the same counter.
    other = Harness(session_factory, database_url, destination, webhook_rate_limit_per_minute=3)
    other.seeded = harness.seeded
    assert other.send_webhook(event_id="evt-other").status_code == 429
    # The window rolls over.
    harness.clock.advance(61)
    assert harness.send_webhook(event_id="evt-later").status_code == 202
    assert len(harness.events()) == 4
