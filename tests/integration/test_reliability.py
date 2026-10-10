"""Retry, dead-letter, replay, and recovery from publish/worker failures."""

from __future__ import annotations

import socket
import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest

from relay_core.messaging.base import DeliveryMessage
from relay_core.messaging.local_queue import LocalQueuePublisher, LocalQueuePump
from relay_core.messaging.publishers import MemoryPublisher
from relay_core.services import ingestion as ingestion_module
from relay_core.services.outbox import OutboxDispatcher
from relay_core.services.processing import Ack
from tests.support import Harness, order


def accept(harness: Harness, name: str = "evt", **payload: Any) -> str:
    return harness.accepted_event_id(harness.send_webhook(order(**payload), event_id=name))


def dead_letter(harness: Harness, name: str = "evt-dl") -> str:
    harness.destination.default.status = 503
    event_id = accept(harness, name)
    assert harness.run_to_completion(event_id).status == "DEAD_LETTERED"
    harness.destination.reset()
    return event_id


# --- acceptance 5: retries --------------------------------------------------


def test_retryable_failure_is_retried_after_backoff_and_then_delivered(harness: Harness) -> None:
    harness.destination.enqueue(500, {"error": "boom"})
    event_id = accept(harness)
    harness.pump()

    event = harness.event(event_id)
    assert event.status == "RETRY_SCHEDULED"
    assert (event.failure_stage, event.failure_category) == ("delivery", "http_5xx")
    assert event.next_attempt_at is not None
    delay = (event.next_attempt_at - harness.clock()).total_seconds()
    assert 0 <= delay <= 2  # full jitter over base_delay * 2**0
    scheduled = harness.outbox(event_id)[-1]
    assert (scheduled.attempt_number, scheduled.status, scheduled.available_at) == (
        2,
        "pending",
        event.next_attempt_at,
    )

    # Not due yet: pumping again must not deliver early.
    harness.pump()
    assert len(harness.destination.requests) == 1

    harness.clock.advance(delay + 0.5)
    harness.pump()
    event = harness.event(event_id)
    assert event.status == "DELIVERED"
    assert event.last_error is None
    assert event.failure_category is None
    assert harness.history(event_id) == [
        "RECEIVED", "PENDING_PUBLISH", "QUEUED", "PROCESSING",
        "RETRY_SCHEDULED", "QUEUED", "PROCESSING", "DELIVERED",
    ]  # fmt: skip
    first, second = harness.attempts(event_id)
    assert (first.status, first.http_status, first.failure_category, first.disposition) == (
        "failed",
        500,
        "http_5xx",
        "retryable",
    )
    assert first.error_message == "destination responded with HTTP 500"
    assert first.response_summary == '{"error":"boom"}'
    assert (second.status, second.http_status, second.attempt_number) == ("succeeded", 200, 2)
    # Same idempotency key on every attempt, so a destination can deduplicate.
    keys = {r["headers"]["idempotency-key"] for r in harness.destination.requests}
    assert keys == {event_id}
    assert [r["headers"]["x-relay-attempt"] for r in harness.destination.requests] == ["1", "2"]


def test_429_honours_retry_after_but_caps_it_at_the_policy_maximum(harness: Harness) -> None:
    harness.destination.enqueue(429, {"error": "slow down"}, headers={"Retry-After": "7"})
    harness.destination.enqueue(429, {"error": "slow down"}, headers={"Retry-After": "86400"})
    event_id = accept(harness)
    harness.pump()
    event = harness.event(event_id)
    assert event.failure_category == "http_429"
    assert harness.attempts(event_id)[0].retry_after_seconds == 7
    assert (event.next_attempt_at - harness.clock()).total_seconds() >= 7  # type: ignore[operator]

    harness.clock.advance(8)
    harness.pump()
    event = harness.event(event_id)
    # A hostile Retry-After cannot park the event: capped at max_delay_seconds (60).
    assert (event.next_attempt_at - harness.clock()).total_seconds() == pytest.approx(60, abs=0.01)  # type: ignore[operator]


def test_timeout_is_retryable_and_flagged_as_ambiguous(harness: Harness) -> None:
    harness.destination.enqueue(200, {"ok": True}, delay=1.6)  # delivery timeout is 1s
    event_id = accept(harness)
    harness.pump()
    (attempt,) = harness.attempts(event_id)
    assert (attempt.status, attempt.failure_category, attempt.http_status) == (
        "failed",
        "timeout",
        None,
    )
    assert "may still have processed" in (attempt.error_message or "")
    assert 900 <= (attempt.duration_ms or 0) <= 1500
    assert harness.event(event_id).status == "RETRY_SCHEDULED"


def test_connection_failure_is_retryable(harness: Harness) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    harness.http_adapter._policy = type(harness.http_adapter._policy)(
        allow_private_hosts=frozenset({f"127.0.0.1:{closed_port}"})
    )
    client = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    update = client.patch(
        f"/api/v1/connections/{harness.seeded.destination_id}",  # type: ignore[union-attr]
        {"config": {"url": f"http://127.0.0.1:{closed_port}/orders", "auth": {"type": "bearer"}}},
    )
    assert update.status_code == 200, update.text
    event_id = accept(harness)
    harness.pump()
    (attempt,) = harness.attempts(event_id)
    assert (attempt.failure_category, attempt.disposition) == ("transport", "retryable")
    assert "ConnectError" in (attempt.error_message or "")
    assert harness.event(event_id).status == "RETRY_SCHEDULED"


def test_malformed_http_response_is_retryable(harness: Harness) -> None:
    harness.destination.enqueue(raw=b"this is not http\r\n\r\n")
    event_id = accept(harness)
    harness.pump()
    (attempt,) = harness.attempts(event_id)
    assert (attempt.failure_category, attempt.disposition) == ("invalid_response", "retryable")
    assert harness.event(event_id).status == "RETRY_SCHEDULED"


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "http_4xx"),
        (404, "http_4xx"),
        (409, "http_4xx"),
        (422, "http_4xx"),
        (401, "auth"),
        (403, "auth"),
    ],
)
def test_permanent_failures_are_dead_lettered_without_retry(
    harness: Harness, status: int, category: str
) -> None:
    harness.destination.enqueue(status, {"error": "no"})
    event_id = accept(harness)
    harness.pump()
    event = harness.event(event_id)
    assert (event.status, event.failure_category, event.attempt_count) == (
        "DEAD_LETTERED",
        category,
        1,
    )
    assert event.dead_lettered_at is not None
    assert len(harness.destination.requests) == 1
    assert all(row.status != "pending" for row in harness.outbox(event_id))


def test_redirect_is_never_followed(harness: Harness) -> None:
    harness.destination.enqueue(
        302, {"moved": True}, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
    )
    event_id = accept(harness)
    harness.pump()
    event = harness.event(event_id)
    assert (event.status, event.failure_category) == ("DEAD_LETTERED", "redirect")
    assert "redirects are not followed" in (event.last_error or "")
    assert len(harness.destination.requests) == 1


def test_destination_response_is_redacted_and_bounded_before_storage(harness: Harness) -> None:
    harness.destination.enqueue(
        400, {"error": "bad", "debug": {"api_key": "sk_live_SECRET", "note": "x" * 5000}}
    )
    event_id = accept(harness)
    harness.pump()
    summary = harness.attempts(event_id)[0].response_summary or ""
    assert "sk_live_SECRET" not in summary
    assert "[REDACTED]" in summary
    assert len(summary) < 2100
    assert "truncated" in summary


# --- acceptance 6: exhaustion ----------------------------------------------


def test_attempts_are_bounded_and_exhaustion_dead_letters_with_a_visible_reason(
    harness: Harness,
) -> None:
    harness.destination.default.status = 503
    event_id = accept(harness)
    event = harness.run_to_completion(event_id)

    assert event.status == "DEAD_LETTERED"
    assert (event.attempt_count, event.max_attempts) == (3, 3)
    assert event.failure_category == "http_5xx"
    assert event.last_error == "destination responded with HTTP 503"
    assert len(harness.destination.requests) == 3
    attempts = harness.attempts(event_id)
    assert [(a.attempt_number, a.status) for a in attempts] == [
        (1, "failed"),
        (2, "failed"),
        (3, "failed"),
    ]
    with harness.session_factory() as session:
        from sqlalchemy import select

        from relay_core.db.models import EventTransition

        last = (
            session.execute(
                select(EventTransition)
                .where(EventTransition.event_id == event.id)
                .order_by(EventTransition.id.desc())
            )
            .scalars()
            .first()
        )
    assert last is not None
    assert (last.to_status, last.reason) == ("DEAD_LETTERED", "attempts_exhausted")

    # Nothing left to publish, and more pumping never produces a fourth attempt.
    harness.clock.advance(3600)
    harness.pump()
    assert len(harness.destination.requests) == 3
    assert all(row.status == "published" for row in harness.outbox(event_id))


# --- acceptance 7: replay ---------------------------------------------------


def test_operator_replay_delivers_and_preserves_history(harness: Harness) -> None:
    event_id = dead_letter(harness)
    before = [(a.cycle, a.attempt_number, a.status) for a in harness.attempts(event_id)]
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]

    response = operator.post(
        f"/api/v1/events/{event_id}/replay",
        {"reason": "destination recovered", "version_mode": "original"},
        headers={"Idempotency-Key": "replay-key-0001"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True
    assert (body["replay"]["cycle"], body["replay"]["from_status"]) == (2, "DEAD_LETTERED")

    harness.pump()
    event = harness.event(event_id)
    assert (event.status, event.delivery_cycle, event.attempt_count) == ("DELIVERED", 2, 1)
    attempts = harness.attempts(event_id)
    # Old attempts are untouched; the new one is linked to the replay operation.
    assert [(a.cycle, a.attempt_number, a.status) for a in attempts[:3]] == before
    assert (attempts[3].cycle, attempts[3].attempt_number, attempts[3].status) == (
        2,
        1,
        "succeeded",
    )
    assert str(attempts[3].replay_operation_id) == body["replay"]["id"]
    assert all(a.replay_operation_id is None for a in attempts[:3])
    assert harness.history(event_id)[-5:] == [
        "REPLAY_REQUESTED",
        "PENDING_PUBLISH",
        "QUEUED",
        "PROCESSING",
        "DELIVERED",
    ]
    assert harness.history(event_id).count("DEAD_LETTERED") == 1
    # Same idempotency key as the original attempts.
    assert harness.destination.requests[-1]["headers"]["idempotency-key"] == event_id

    (entry,) = harness.audit("event.replay")
    assert entry.entity_id == event_id
    assert entry.details["reason"] == "destination recovered"
    assert entry.details["from_status"] == "DEAD_LETTERED"
    assert entry.actor_user_id is not None


def test_replay_is_idempotent_on_its_key(harness: Harness) -> None:
    event_id = dead_letter(harness)
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    url, body = f"/api/v1/events/{event_id}/replay", {"reason": "retry please"}
    first = operator.post(url, body, headers={"Idempotency-Key": "same-key-123"})
    second = operator.post(url, body, headers={"Idempotency-Key": "same-key-123"})
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["replay"]["id"] == second.json()["replay"]["id"]
    assert (first.json()["created"], second.json()["created"]) == (True, False)
    harness.pump()
    assert len(harness.destination.requests) == 1
    assert len(harness.audit("event.replay")) == 1
    assert harness.event(event_id).delivery_cycle == 2

    other_event = dead_letter(harness, "evt-other")
    reused = operator.post(
        f"/api/v1/events/{other_event}/replay", body, headers={"Idempotency-Key": "same-key-123"}
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "idempotency_key_reused"


def test_concurrent_replays_with_different_keys_replay_once(harness: Harness) -> None:
    event_id = dead_letter(harness)
    clients = [harness.login(harness.seeded.operator) for _ in range(4)]  # type: ignore[union-attr]
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def go(index: int) -> None:
        barrier.wait()
        codes.append(
            clients[index]
            .post(
                f"/api/v1/events/{event_id}/replay",
                {"reason": "race"},
                headers={"Idempotency-Key": f"race-key-{index:04d}"},
            )
            .status_code
        )

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [202, 409, 409, 409]
    harness.pump()
    assert len(harness.destination.requests) == 1
    assert harness.event(event_id).delivery_cycle == 2


def test_replay_rules(harness: Harness) -> None:
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    delivered = accept(harness, "evt-ok")
    harness.pump()
    response = operator.post(
        f"/api/v1/events/{delivered}/replay",
        {"reason": "again"},
        headers={"Idempotency-Key": "key-delivered-1"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_replayable"

    failed = dead_letter(harness, "evt-f")
    url = f"/api/v1/events/{failed}/replay"
    assert (
        operator.post(url, {"reason": "no key"}).json()["error"]["code"]
        == "idempotency_key_required"
    )
    assert (
        operator.post(
            url, {"reason": "x"}, headers={"Idempotency-Key": "key-short-reason"}
        ).status_code
        == 422
    )
    assert (
        operator.post(url, {"reason": "short key"}, headers={"Idempotency-Key": "abc"}).json()[
            "error"
        ]["code"]
        == "invalid_idempotency_key"
    )
    assert (
        operator.post(
            f"/api/v1/events/{uuid.uuid4()}/replay",
            {"reason": "missing"},
            headers={"Idempotency-Key": "key-missing-01"},
        ).status_code
        == 404
    )
    assert harness.event(failed).status == "DEAD_LETTERED"
    assert harness.audit("event.replay") == []


def test_replay_with_current_version_applies_a_fixed_mapping(harness: Harness) -> None:
    from tests.support import ORDER_CONFIG

    event_id = accept(harness, "evt-fix", qty="two")
    harness.pump()
    assert harness.event(event_id).status == "VALIDATION_FAILED"
    original_version = harness.event(event_id).integration_version_id

    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    fixed = {
        **ORDER_CONFIG,
        "mappings": [
            m if m["target"] != "quantity" else {"target": "quantity", "const": 1}
            for m in ORDER_CONFIG["mappings"]
        ],
    }
    assert (
        admin.patch(
            f"/api/v1/integrations/{harness.seeded.integration_id}",
            {"config": fixed, "change_note": "tolerate text qty"},
        ).status_code
        == 200
    )  # type: ignore[union-attr]

    # Replaying against the original version reproduces the failure...
    assert (
        admin.post(
            f"/api/v1/events/{event_id}/replay",
            {"reason": "check", "version_mode": "original"},
            headers={"Idempotency-Key": "replay-original-1"},
        ).status_code
        == 202
    )
    harness.pump()
    assert harness.event(event_id).status == "VALIDATION_FAILED"
    # ...and against the current version it succeeds.
    assert (
        admin.post(
            f"/api/v1/events/{event_id}/replay",
            {"reason": "mapping fixed", "version_mode": "current"},
            headers={"Idempotency-Key": "replay-current-1"},
        ).status_code
        == 202
    )
    harness.pump()
    event = harness.event(event_id)
    assert event.status == "DELIVERED"
    assert event.integration_version_id != original_version
    assert event.delivery_cycle == 3
    assert harness.destination.requests[-1]["json"]["quantity"] == 1
    assert harness.attempts(event_id)[-1].integration_version_id == event.integration_version_id


# --- acceptance 9: publish failure and worker failure -----------------------


def test_publish_failure_does_not_lose_the_event(harness: Harness) -> None:
    harness.publisher.fail_next = 2  # the inline publish and the first dispatcher pass
    response = harness.send_webhook(order(), event_id="evt-pub")
    assert response.status_code == 202  # accepted: the event and outbox row are committed
    event_id = harness.accepted_event_id(response)
    assert harness.event(event_id).status == "PENDING_PUBLISH"
    (row,) = harness.outbox(event_id)
    assert (row.status, row.publish_attempts) == ("pending", 1)
    assert "simulated broker outage" in (row.last_error or "")
    assert row.available_at > harness.clock()  # backed off, not hot-looping

    harness.pump()  # backoff has not elapsed
    assert harness.outbox(event_id)[0].publish_attempts == 1
    harness.clock.advance(5)
    harness.dispatcher.dispatch_due()  # second failure
    assert harness.outbox(event_id)[0].publish_attempts == 2
    assert harness.event(event_id).status == "PENDING_PUBLISH"

    harness.clock.advance(10)
    harness.pump()
    assert harness.event(event_id).status == "DELIVERED"
    (row,) = harness.outbox(event_id)
    assert (row.status, row.publish_attempts, row.last_error) == ("published", 3, None)
    assert row.broker_message_id is not None
    assert len(harness.destination.requests) == 1


def test_event_and_outbox_row_commit_atomically(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database write failed mid-transaction")

    monkeypatch.setattr(ingestion_module, "schedule_delivery", explode)
    with pytest.raises(RuntimeError):
        harness.send_webhook(order(), event_id="evt-atomic")
    # No half-written state: the event row rolled back with the outbox insert.
    assert harness.events() == []
    assert harness.outbox() == []
    monkeypatch.undo()
    assert (
        harness.send_webhook(order(), event_id="evt-atomic").json()["results"][0]["outcome"]
        == "accepted"
    )


def test_dispatcher_crash_after_publish_republishes_and_worker_drops_the_duplicate(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.ingestion._dispatcher = None
    event_id = accept(harness, "evt-crash")
    assert harness.event(event_id).status == "PENDING_PUBLISH"

    real_mark = OutboxDispatcher._mark_published
    monkeypatch.setattr(
        OutboxDispatcher,
        "_mark_published",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("db gone")),
    )
    stats = harness.dispatcher.dispatch_due()
    assert (stats.claimed, stats.published, stats.failed) == (1, 0, 1)
    # The broker has the message but the row still says pending, under a lease.
    assert len(harness.publisher.messages) == 1
    (row,) = harness.outbox(event_id)
    assert row.status == "pending"
    assert row.lease_expires_at is not None

    monkeypatch.setattr(OutboxDispatcher, "_mark_published", real_mark)
    assert harness.dispatcher.dispatch_due().claimed == 0  # lease still held

    harness.clock.advance(harness.settings.outbox_lease_seconds + 1)
    assert harness.dispatcher.dispatch_due().published == 1
    assert len(harness.publisher.messages) == 2  # the message was published twice

    responses = harness.deliver_published()
    assert [r.json()["detail"] for r in responses] == ["delivered", "duplicate_message"]
    assert all(r.status_code == 200 for r in responses)
    assert len(harness.destination.requests) == 1
    assert len(harness.attempts(event_id)) == 1
    assert harness.event(event_id).status == "DELIVERED"


def test_concurrent_dispatchers_never_publish_the_same_row_twice(harness: Harness) -> None:
    harness.ingestion._dispatcher = None
    for index in range(60):
        accept(harness, f"evt-{index}", order_no=f"A-{index}")
    publishers = [MemoryPublisher() for _ in range(4)]
    dispatchers = [
        OutboxDispatcher(harness.session_factory, p, batch_size=25, clock=harness.clock)
        for p in publishers
    ]
    barrier = threading.Barrier(4)

    def run(dispatcher: OutboxDispatcher) -> None:
        barrier.wait()
        while dispatcher.dispatch_due().claimed:
            pass

    threads = [threading.Thread(target=run, args=(d,)) for d in dispatchers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    published = [data["event_id"] for p in publishers for data, _ in p.messages]
    assert len(published) == 60
    assert len(set(published)) == 60  # SKIP LOCKED: no row claimed by two dispatchers
    assert sum(1 for p in publishers if p.messages) > 1  # the work really was shared
    assert all(row.status == "published" for row in harness.outbox())
    assert {e.status for e in harness.events()} == {"QUEUED"}


def test_worker_crash_mid_attempt_is_recovered_when_the_lease_expires(harness: Harness) -> None:
    event_id = accept(harness, "evt-lease")
    data, _ = harness.publisher.messages.pop(0)
    message = DeliveryMessage.from_data(data)

    # Step 1 only: the worker claims the event, then "dies" before delivering.
    claimed = harness.processor._claim(message)
    assert not hasattr(claimed, "ack")
    event = harness.event(event_id)
    assert event.status == "PROCESSING"
    assert event.processing_lease_expires_at is not None
    assert harness.attempts(event_id)[0].status == "in_progress"

    # The broker redelivers while the lease is live: do not start a second delivery.
    redelivery = harness.push(data)
    assert redelivery.status_code == 503
    assert redelivery.json() == {"status": "retry", "detail": "in_progress_elsewhere"}
    assert harness.processor.reap_expired_leases() == 0
    assert harness.destination.requests == []

    harness.clock.advance(harness.settings.processing_lease_margin_seconds + 5)
    assert harness.processor.reap_expired_leases() == 1
    event = harness.event(event_id)
    assert event.status == "RETRY_SCHEDULED"
    (interrupted,) = harness.attempts(event_id)
    assert (interrupted.status, interrupted.failure_category) == ("failed", "worker_interrupted")
    assert "may or may not have received" in (interrupted.error_message or "")

    # A late result from the dead worker must not overwrite the recovery.
    from relay_core.adapters.base import DeliveryResult
    from relay_core.domain.retry import Disposition

    late = harness.processor._complete(
        message, claimed.attempt_id, DeliveryResult(Disposition.SUCCESS, 5, http_status=200)
    )  # type: ignore[union-attr]
    assert (late.ack, late.detail) == (Ack.ACK, "attempt_already_finalised")
    assert harness.event(event_id).status == "RETRY_SCHEDULED"

    assert harness.run_to_completion(event_id).status == "DELIVERED"
    assert [a.status for a in harness.attempts(event_id)] == ["failed", "succeeded"]
    assert len(harness.destination.requests) == 1


def test_redelivery_after_lease_expiry_recovers_without_the_reaper(harness: Harness) -> None:
    event_id = accept(harness, "evt-lease2")
    data, _ = harness.publisher.messages.pop(0)
    harness.processor._claim(DeliveryMessage.from_data(data))
    harness.clock.advance(120)
    response = harness.push(data)
    assert (response.status_code, response.json()["detail"]) == (
        200,
        "recovered_interrupted_attempt",
    )
    assert harness.event(event_id).status == "RETRY_SCHEDULED"


def test_interrupted_attempts_count_toward_the_attempt_ceiling(harness: Harness) -> None:
    """A worker that crashes every time cannot loop forever."""
    event_id = accept(harness, "evt-poison")
    for _ in range(10):
        if not harness.publisher.messages:
            harness.clock.advance(120)
            harness.dispatcher.dispatch_due()
        if not harness.publisher.messages:
            break
        data, _ = harness.publisher.messages.pop(0)
        harness.processor._claim(DeliveryMessage.from_data(data))
        harness.clock.advance(120)
        harness.processor.reap_expired_leases()
    event = harness.event(event_id)
    assert (event.status, event.attempt_count) == ("DEAD_LETTERED", 3)
    assert event.failure_category == "worker_interrupted"
    assert harness.destination.requests == []


def test_broker_dead_letter_notification_dead_letters_the_event(harness: Harness) -> None:
    event_id = accept(harness, "evt-dlq")
    data, _ = harness.publisher.messages.pop(0)
    response = harness.push(data, path="/internal/pubsub/dead-letter")
    assert (response.status_code, response.json()["detail"]) == (200, "dead_lettered")
    event = harness.event(event_id)
    assert (event.status, event.failure_category) == ("DEAD_LETTERED", "queue_exhausted")
    # Replaying it later still works.
    assert (
        harness.push(data, path="/internal/pubsub/dead-letter").json()["detail"] == "stale_message"
    )


def test_worker_acknowledges_messages_it_can_never_process(harness: Harness) -> None:
    ghost = DeliveryMessage(uuid.uuid4(), harness.seeded.workspace_id, 1, 1, "corr-ghost-1")  # type: ignore[union-attr]
    assert harness.push(ghost.to_data()).json() == {"status": "ok", "detail": "unknown_event"}
    for bad in (
        {"v": 99},
        {"v": 1, "event_id": "nope"},
        {
            "v": 1,
            "event_id": str(uuid.uuid4()),
            "workspace_id": str(uuid.uuid4()),
            "cycle": 0,
            "attempt": 1,
            "correlation_id": "c" * 8,
        },
    ):
        response = harness.push(bad)
        assert (response.status_code, response.json()["status"]) == (200, "discarded")
    garbage = harness.worker.post(
        "/internal/pubsub/push",
        json={"message": {"data": "!!not-base64!!"}},
        headers={"x-relay-internal-token": "test-internal-token"},
    )
    assert (garbage.status_code, garbage.json()["status"]) == (200, "discarded")

    # A message addressed to another workspace's event id is not processed.
    event_id = accept(harness, "evt-ws")
    data, _ = harness.publisher.messages.pop(0)
    assert (
        harness.push({**data, "workspace_id": str(uuid.uuid4())}).json()["detail"]
        == "unknown_event"
    )
    assert harness.event(event_id).status == "QUEUED"


def test_worker_endpoints_require_internal_authentication(harness: Harness) -> None:
    event_id = accept(harness, "evt-auth")
    data, _ = harness.publisher.messages[0]
    for token in (None, "wrong-token", ""):
        for path in ("/internal/pubsub/push", "/internal/pubsub/dead-letter"):
            assert harness.push(data, path=path, token=token).status_code == 401
        headers = {"x-relay-internal-token": token} if token is not None else {}
        assert harness.worker.post("/internal/outbox/dispatch", headers=headers).status_code == 401
    assert harness.event(event_id).status == "QUEUED"
    assert harness.destination.requests == []


def test_dispatch_endpoint_publishes_and_recovers_in_one_bounded_pass(harness: Harness) -> None:
    harness.ingestion._dispatcher = None
    for index in range(3):
        accept(harness, f"evt-d{index}", order_no=f"A-{index}")
    response = harness.worker.post(
        "/internal/outbox/dispatch", headers={"x-relay-internal-token": "test-internal-token"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "claimed": 3,
        "published": 3,
        "publish_failed": 0,
        "leases_recovered": 0,
        "payloads_purged": 0,
    }
    harness.deliver_published()
    assert {e.status for e in harness.events()} == {"DELIVERED"}


# --- pause / resume / disable ----------------------------------------------


def test_paused_integration_holds_events_until_resumed(harness: Harness) -> None:
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    integration = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    in_flight = accept(harness, "evt-inflight")  # queued before the pause

    assert operator.post(f"{integration}/pause").json()["integration"]["status"] == "paused"
    response = harness.send_webhook(order("A-2"), event_id="evt-held")
    assert response.status_code == 202
    assert response.json()["results"][0]["outcome"] == "held"
    held = response.json()["results"][0]["event_id"]

    harness.pump()
    assert harness.event(held).status == "HELD"
    assert harness.event(in_flight).status == "HELD"  # the worker held it instead of delivering
    assert harness.destination.requests == []

    resumed = operator.post(f"{integration}/resume").json()
    assert (resumed["integration"]["status"], resumed["held_events_affected"]) == ("active", 2)
    harness.pump()
    assert harness.event(held).status == "DELIVERED"
    assert harness.event(in_flight).status == "DELIVERED"
    assert len(harness.destination.requests) == 2
    assert harness.history(held) == [
        "RECEIVED",
        "HELD",
        "PENDING_PUBLISH",
        "QUEUED",
        "PROCESSING",
        "DELIVERED",
    ]
    assert [a.action for a in harness.audit("integration.")][-2:] == [
        "integration.pause",
        "integration.resume",
    ]


def test_disabling_an_integration_dead_letters_held_and_in_flight_events(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    integration = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    in_flight = accept(harness, "evt-a")
    admin.post(f"{integration}/pause")
    held = harness.send_webhook(order("A-2"), event_id="evt-b").json()["results"][0]["event_id"]

    result = admin.post(f"{integration}/disable").json()
    assert (result["integration"]["status"], result["held_events_affected"]) == ("disabled", 1)
    assert harness.event(held).status == "DEAD_LETTERED"
    harness.pump()
    event = harness.event(in_flight)
    assert (event.status, event.failure_category) == ("DEAD_LETTERED", "integration_disabled")
    assert harness.destination.requests == []
    # A disabled integration no longer accepts events.
    assert (
        harness.send_webhook(order("A-3"), event_id="evt-c").json()["error"]["code"]
        == "no_matching_integration"
    )
    # And its dead letters cannot be replayed until it is re-activated.
    blocked = admin.post(
        f"/api/v1/events/{held}/replay",
        {"reason": "try"},
        headers={"Idempotency-Key": "disabled-replay-1"},
    )
    assert blocked.json()["error"]["code"] == "integration_not_active"
    assert admin.post(f"{integration}/activate").status_code == 200
    assert (
        admin.post(
            f"/api/v1/events/{held}/replay",
            {"reason": "re-enabled"},
            headers={"Idempotency-Key": "disabled-replay-2"},
        ).status_code
        == 202
    )
    harness.pump()
    assert harness.event(held).status == "DELIVERED"


# --- local development queue ------------------------------------------------


def test_local_queue_redelivers_with_backoff_then_dead_letters(harness: Harness) -> None:
    """The dev broker reproduces the Pub/Sub behaviour the worker depends on."""
    publisher = LocalQueuePublisher(
        harness.session_factory, "relay-deliveries", clock=harness.clock
    )
    harness.dispatcher._publisher = publisher
    event_id = accept(harness, "evt-local")
    assert harness.event(event_id).status == "QUEUED"

    calls: list[tuple[str, int | None]] = []
    worker_down = True

    def transport(path: str, envelope: dict[str, Any]) -> int:
        calls.append((path, envelope.get("deliveryAttempt")))
        if worker_down and path.endswith("/push"):
            raise ConnectionError("worker unreachable")
        return harness.worker.post(
            path, json=envelope, headers={"x-relay-internal-token": "test-internal-token"}
        ).status_code

    pump = LocalQueuePump(
        harness.session_factory,
        transport,
        topic="relay-deliveries",
        max_delivery_attempts=3,
        clock=harness.clock,
    )
    assert pump.tick().redelivery_scheduled == 1
    assert pump.tick().delivered == 0  # backing off
    harness.clock.advance(2)
    assert pump.tick().redelivery_scheduled == 1
    harness.clock.advance(5)
    stats = pump.tick()  # third failure reaches the limit -> dead-letter endpoint
    assert stats.dead_lettered == 1
    assert calls == [
        ("/internal/pubsub/push", 1),
        ("/internal/pubsub/push", 2),
        ("/internal/pubsub/push", 3),
        ("/internal/pubsub/dead-letter", 3),
    ]
    event = harness.event(event_id)
    assert (event.status, event.failure_category) == ("DEAD_LETTERED", "queue_exhausted")
    harness.clock.advance(600)
    assert pump.tick() == type(stats)()  # a dead message is never pushed again


def test_local_queue_delivers_through_the_real_worker_endpoint(harness: Harness) -> None:
    harness.dispatcher._publisher = LocalQueuePublisher(
        harness.session_factory, "relay-deliveries", clock=harness.clock
    )
    event_id = accept(harness, "evt-local-ok")

    def transport(path: str, envelope: dict[str, Any]) -> int:
        return harness.worker.post(
            path, json=envelope, headers={"x-relay-internal-token": "test-internal-token"}
        ).status_code

    pump = LocalQueuePump(
        harness.session_factory, transport, topic="relay-deliveries", clock=harness.clock
    )
    assert pump.tick().delivered == 1
    assert harness.event(event_id).status == "DELIVERED"
    assert pump.tick().delivered == 0


def test_retention_purges_payload_bodies_but_keeps_history_and_blocks_replay(
    harness: Harness,
) -> None:
    from relay_core.services.maintenance import purge_expired_payloads

    event_id = dead_letter(harness, "evt-old")
    assert (
        purge_expired_payloads(harness.session_factory, retention_days=30, now=harness.clock()) == 0
    )
    later = harness.clock() + timedelta(days=31)
    assert (
        purge_expired_payloads(harness.session_factory, retention_days=0, now=later) == 0
    )  # disabled
    assert purge_expired_payloads(harness.session_factory, retention_days=30, now=later) == 1
    event = harness.event(event_id)
    assert event.payload == {"_purged": True}
    assert event.payload_purged_at is not None
    assert len(harness.attempts(event_id)) == 3  # history survives
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    blocked = operator.post(
        f"/api/v1/events/{event_id}/replay",
        {"reason": "too late"},
        headers={"Idempotency-Key": "purged-replay-01"},
    )
    assert blocked.json()["error"]["code"] == "payload_purged"
    assert operator.get(f"/api/v1/events/{event_id}").json()["replayable"] is False
