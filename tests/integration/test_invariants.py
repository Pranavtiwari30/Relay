"""Cross-cutting invariants checked against the database after realistic activity,
plus the investigation eval fixtures run in deterministic mode."""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise

import pytest
from sqlalchemy import select

from evals.run_evals import run_fixture
from evals.scoring import load_fixtures
from relay_core.db.models import DeliveryAttempt, Event, EventTransition, OutboxMessage
from relay_core.domain.states import TERMINAL, TRANSITIONS, EventStatus
from tests.support import Harness, order


def exercise_everything(harness: Harness) -> None:
    """Drive events down every branch of the state machine."""
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    integration = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    harness.send_webhook(order("A-ok"), event_id="ok")
    harness.send_webhook(order("A-ok"), event_id="ok")  # duplicate
    harness.send_webhook({"qty": 1}, event_id="invalid")
    harness.send_webhook(order(qty="x"), event_id="transform")
    harness.send_webhook(order(status="draft"), event_id="filtered")
    harness.pump()
    harness.destination.enqueue(500)
    harness.send_webhook(order("A-retry"), event_id="retry-then-ok")
    harness.pump()
    harness.destination.default.status = 503
    dead = harness.accepted_event_id(harness.send_webhook(order("A-dead"), event_id="dead"))
    harness.run_to_completion(dead)
    harness.destination.default.status = 400
    perm = harness.accepted_event_id(harness.send_webhook(order("A-perm"), event_id="perm"))
    harness.run_to_completion(perm)
    # Make the destination healthy again without discarding its request log.
    harness.destination.default.status = 200
    harness.destination.script.clear()
    for _ in range(4):
        harness.clock.advance(30)
        harness.pump()
    operator.post(
        f"/api/v1/events/{dead}/replay",
        {"reason": "recovered"},
        headers={"Idempotency-Key": "invariant-replay-1"},
    )
    harness.pump()
    operator.post(f"{integration}/pause")
    harness.send_webhook(order("A-held"), event_id="held")
    harness.send_webhook(order("A-held2"), event_id="held2")
    operator.post(f"{integration}/resume")
    harness.pump()
    harness.send_webhook(order("A-flight"), event_id="in-flight")  # left queued


def test_every_status_change_is_recorded_and_legal(harness: Harness) -> None:
    exercise_everything(harness)
    with harness.session_factory() as session:
        events = {e.id: e for e in session.execute(select(Event)).scalars()}
        history: dict[object, list[EventTransition]] = defaultdict(list)
        for row in session.execute(select(EventTransition).order_by(EventTransition.id)).scalars():
            history[row.event_id].append(row)
        attempts: dict[object, list[DeliveryAttempt]] = defaultdict(list)
        for attempt in session.execute(
            select(DeliveryAttempt).order_by(DeliveryAttempt.cycle, DeliveryAttempt.attempt_number)
        ).scalars():
            attempts[attempt.event_id].append(attempt)
        outbox = list(session.execute(select(OutboxMessage)).scalars())

    assert len(events) == 10  # the duplicate send created no row
    seen_statuses = set()
    for event_id, event in events.items():
        rows = history[event_id]
        # 1. History starts at RECEIVED and its last entry is the current status.
        assert (rows[0].from_status, rows[0].to_status) == (None, "RECEIVED")
        assert rows[-1].to_status == event.status
        # 2. Each step continues from the previous one along a legal edge.
        for previous, current in pairwise(rows):
            assert current.from_status == previous.to_status
            assert EventStatus(current.to_status) in TRANSITIONS[EventStatus(previous.to_status)]
            assert current.created_at >= previous.created_at
        seen_statuses.update(row.to_status for row in rows)
        # 3. Timestamps and counters agree with the status.
        assert (event.delivered_at is not None) == (event.status == "DELIVERED")
        assert event.processing_lease_expires_at is None or event.status == "PROCESSING"
        assert event.next_attempt_at is None or event.status == "RETRY_SCHEDULED"
        mine = [a for a in attempts[event_id] if a.cycle == event.delivery_cycle]
        assert event.attempt_count == len(mine) <= event.max_attempts
        # 4. Attempt numbering is gapless per cycle and nothing is left in progress.
        by_cycle: dict[int, list[int]] = defaultdict(list)
        for attempt in attempts[event_id]:
            by_cycle[attempt.cycle].append(attempt.attempt_number)
            assert attempt.status != "in_progress"
            assert attempt.idempotency_key == str(event_id)
        for numbers in by_cycle.values():
            assert numbers == list(range(1, len(numbers) + 1))
        # 5. A delivered event has exactly one successful attempt, and it is the last.
        succeeded = [a for a in attempts[event_id] if a.status == "succeeded"]
        assert len(succeeded) == (1 if event.status == "DELIVERED" else 0)
        if succeeded:
            assert succeeded[0] is attempts[event_id][-1]

    # The scenario really did visit every state.
    assert seen_statuses == {s.value for s in EventStatus}
    # 6. No pending outbox row for an event that has finished.
    finished = {eid for eid, e in events.items() if EventStatus(e.status) in TERMINAL}
    assert not [row for row in outbox if row.status == "pending" and row.event_id in finished]
    # 7. The destination received exactly one request per recorded attempt.
    assert len(harness.destination.requests) == sum(len(a) for a in attempts.values())


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda f: f["name"])
def test_deterministic_investigator_passes_every_eval_fixture(
    harness: Harness, fixture: dict
) -> None:  # type: ignore[type-arg]
    result = run_fixture(harness, fixture)
    assert result.mode == "deterministic"
    assert result.failed_checks == [], result.notes
