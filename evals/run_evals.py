"""Run the investigation evals and print a scorecard.

    uv run python -m evals.run_evals            # deterministic investigator (no model, free)
    uv run python -m evals.run_evals --live     # whichever providers RELAY_AI_PROVIDERS configures

Each fixture builds a real failing event in the *test* database (through the
real ingestion and delivery path against a local stub destination), runs an
investigation, and grades the report. ``--live`` calls a real model and may
cost money with a hosted provider; it is never run in CI.

These numbers describe behaviour on these nine fixtures only. They are not an
accuracy claim about investigations in general.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from sqlalchemy import func, select, text

from evals.scoring import Score, load_fixtures, score
from relay_core.ai.service import build_providers
from relay_core.config import Settings
from relay_core.db import migrate
from relay_core.db.models import Base, Event, IntegrationVersion, OutboxMessage
from relay_core.db.session import build_engine, build_session_factory
from tests.support import Harness, StubDestination, order

DEFAULT_URL = "postgresql+psycopg://relay@127.0.0.1:54329/relay_test"


def build_scenario(harness: Harness, fixture: dict[str, Any]) -> str:
    """Create the event the fixture describes and return its id."""
    setup = fixture["setup"]
    if setup.get("good_example_first"):
        harness.send_webhook(order("A-good"), event_id=f"{fixture['name']}-good")
        harness.pump()
    harness.destination.default.status = setup.get("default_status", 200)
    if "default_body" in setup:
        harness.destination.default.body = setup["default_body"]
    payload = setup.get("payload") or order(**setup.get("payload_overrides", {}))
    response = harness.send_webhook(payload, event_id=fixture["name"])
    event_id = str(response.json()["results"][0]["event_id"])
    harness.run_to_completion(event_id)
    harness.destination.default.status = 200
    return event_id


def valid_references(harness: Harness, event_id: str) -> set[str]:
    refs = {f"event:{event_id}"}
    refs |= {f"attempt:{attempt.id}" for attempt in harness.attempts(event_id)}
    event = harness.event(event_id)
    refs.add(f"integration_version:{event.integration_version_id}")
    with harness.session_factory() as session:
        refs |= {
            f"transition:{row[0]}"
            for row in session.execute(
                text("SELECT id FROM event_transitions WHERE event_id = :e"), {"e": event_id}
            )
        }
        refs |= {
            f"connection:{row[0]}" for row in session.execute(text("SELECT id FROM connections"))
        }
        refs |= {
            f"example_event:{row[0]}"
            for row in session.execute(text("SELECT id FROM events WHERE status = 'DELIVERED'"))
        }
    return refs


def snapshot(harness: Harness) -> tuple[Any, ...]:
    with harness.session_factory() as session:
        return (
            [
                (str(e.id), e.status, e.delivery_cycle, e.attempt_count)
                for e in session.execute(select(Event).order_by(Event.id)).scalars()
            ],
            session.execute(select(func.count()).select_from(OutboxMessage)).scalar_one(),
            session.execute(select(func.count()).select_from(IntegrationVersion)).scalar_one(),
            len(harness.destination.requests),
        )


def run_fixture(harness: Harness, fixture: dict[str, Any]) -> Score:
    event_id = build_scenario(harness, fixture)
    actual_status = harness.event(event_id).status
    before = snapshot(harness)
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    investigation = operator.post(f"/api/v1/events/{event_id}/investigations").json()
    result = score(
        fixture,
        investigation,
        valid_refs=valid_references(harness, event_id),
        state_before=before,
        state_after=snapshot(harness),
    )
    result.checks["scenario_reached_expected_status"] = (
        actual_status == fixture["expect"]["event_status"]
    )
    if investigation.get("fallback_reason"):
        result.notes.append(f"fell back: {investigation['fallback_reason']}")
    return result


def run_all(
    session_factory: Any,
    database_url: str,
    destination: StubDestination,
    *,
    providers: list[Any] | None = None,
) -> list[Score]:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    results = []
    for fixture in load_fixtures():
        with session_factory.kw["bind"].begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        destination.reset()
        harness = Harness(
            session_factory,
            database_url,
            destination,
            providers=providers,
            ai_total_timeout_seconds=120,
            ai_request_timeout_seconds=60,
        )
        harness.seed()
        try:
            results.append(run_fixture(harness, fixture))
        finally:
            harness.http_adapter.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--live", action="store_true", help="use the model providers configured in the environment"
    )
    args = parser.parse_args()
    database_url = os.environ.get("RELAY_TEST_DATABASE_URL", DEFAULT_URL)
    if "test" not in database_url.rsplit("/", 1)[-1]:
        sys.exit("refusing to run: evals truncate tables, so the database name must contain 'test'")

    providers: list[Any] | None = None
    if args.live:
        providers = list(build_providers(Settings()))
        if not providers:
            sys.exit("--live needs RELAY_AI_PROVIDERS (and credentials) to be configured")
        print("Live providers:", ", ".join(f"{p.name} ({p.model})" for p in providers))

    engine = build_engine(database_url)
    migrate.upgrade(database_url)
    destination = StubDestination()
    try:
        results = run_all(
            build_session_factory(engine), database_url, destination, providers=providers
        )
    finally:
        destination.close()
        engine.dispose()

    width = max(len(r.fixture) for r in results)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        detail = "" if r.passed else "  failed: " + ", ".join(r.failed_checks)
        print(f"{status}  {r.fixture:<{width}}  [{r.mode}]{detail}")
        for note in r.notes:
            print(f"      note: {note}")
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} fixtures passed every check.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
