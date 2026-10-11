"""Configuration lifecycle, event explorer, dashboard, audit and API conventions."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.support import ORDER_CONFIG, Harness, order

# --- acceptance 1: create, validate, activate -------------------------------


def test_integration_is_created_validated_and_activated(harness: Harness) -> None:
    s = harness.seeded
    assert s is not None
    admin = harness.login(s.admin)
    draft = copy.deepcopy(ORDER_CONFIG)
    draft["mappings"] = [{"target": "quantity", "source": "qty..bad", "convert": "integer"}]
    created = admin.post(
        "/api/v1/integrations",
        {
            "name": "Refunds",
            "description": "demo",
            "source_connection_id": str(s.source_id),
            "destination_connection_id": str(s.destination_id),
            "config": draft,
        },
    )
    assert created.status_code == 201, created.text
    integration = created.json()
    assert (integration["status"], integration["current_version"]) == ("draft", 1)
    url = f"/api/v1/integrations/{integration['id']}"

    # A draft does not receive events.
    harness.send_webhook(order(), event_id="while-draft")
    assert all(str(e.integration_id) != integration["id"] for e in harness.events())

    report = admin.post(f"{url}/validate").json()
    assert report["valid"] is False
    problems = {(i["code"], i["path"]) for i in report["issues"] if i["severity"] == "error"}
    assert ("invalid_path", "mappings[0].source") in problems
    assert ("unmapped_required_field", "output_schema.required.order_id") in problems

    blocked = admin.post(f"{url}/activate")
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "validation_failed"
    assert {d["path"] for d in blocked.json()["error"]["details"]} >= {"mappings[0].source"}
    assert admin.get(url).json()["status"] == "draft"

    # Validate a proposed fix without saving it, then save and activate.
    assert admin.post(f"{url}/validate", {"config": ORDER_CONFIG}).json()["valid"] is True
    assert admin.get(url).json()["current_version"] == 1
    fixed = admin.patch(url, {"config": ORDER_CONFIG, "change_note": "fix mappings"}).json()
    assert fixed["current_version"] == 2
    activated = admin.post(f"{url}/activate")
    assert activated.status_code == 200
    assert activated.json()["integration"]["status"] == "active"

    versions = admin.get(f"{url}/versions").json()
    assert [(v["version"], v["is_current"], v["change_note"]) for v in versions] == [
        (2, True, "fix mappings"),
        (1, False, "Initial version"),
    ]
    assert versions[1]["config"]["mappings"][0]["source"] == "qty..bad"  # history is immutable

    actions = [a.action for a in harness.audit("integration.") if a.entity_id == integration["id"]]
    assert actions == ["integration.create", "integration.update", "integration.activate"]


def test_every_event_records_the_configuration_version_that_processed_it(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    url = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    first = harness.accepted_event_id(harness.send_webhook(order(), event_id="v1-event"))
    changed = copy.deepcopy(ORDER_CONFIG)
    changed["mappings"][-1] = {"target": "source", "const": "v2"}
    assert admin.patch(url, {"config": changed}).json()["current_version"] == 2
    # Saving an identical configuration does not mint a new version.
    assert admin.patch(url, {"config": changed}).json()["current_version"] == 2
    second = harness.accepted_event_id(harness.send_webhook(order("A-2"), event_id="v2-event"))
    harness.pump()

    # The in-flight event keeps the version it was accepted under.
    details = {e: admin.get(f"/api/v1/events/{e}").json() for e in (first, second)}
    assert (details[first]["integration_version"], details[second]["integration_version"]) == (1, 2)
    assert details[first]["transformed_payload"]["source"] == "test"
    assert details[second]["transformed_payload"]["source"] == "v2"


def test_live_integration_rejects_a_configuration_that_could_not_be_activated(
    harness: Harness,
) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    url = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    broken = copy.deepcopy(ORDER_CONFIG)
    broken["mappings"] = [{"target": "quantity", "source": "qty"}]
    response = admin.patch(url, {"config": broken})
    assert response.status_code == 422
    assert admin.get(url).json()["current_version"] == 1
    for bad in (
        {"mappings": [{"target": "a"}]},
        {"mappings": [{"target": "a", "source": "b", "convert": "eval"}]},
        {"retry": {"max_attempts": 500}, "passthrough": True},
        {"surprise": 1},
    ):
        rejected = admin.patch(url, {"config": bad})
        assert rejected.status_code == 422, bad
        assert rejected.json()["error"]["details"], bad


def test_activation_requires_resolvable_secrets_and_active_connections(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    url = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    admin.post(f"{url}/disable")
    del harness.env["RELAY_SECRET_TEST_DEST"]
    blocked = admin.post(f"{url}/activate")
    assert blocked.status_code == 422
    assert ("secret_unavailable", "destination_connection.secret_ref") in {
        (d["code"], d["path"]) for d in blocked.json()["error"]["details"]
    }
    assert "dest_test_token" not in blocked.text  # the value is never echoed
    harness.env["RELAY_SECRET_TEST_DEST"] = "restored-token-value"
    admin.patch(f"/api/v1/connections/{harness.seeded.source_id}", {"is_active": False})  # type: ignore[union-attr]
    assert "connection_inactive" in {
        d["code"] for d in admin.post(f"{url}/activate").json()["error"]["details"]
    }


def test_preview_dry_runs_a_configuration_without_side_effects(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    ready = viewer.post(
        "/api/v1/integrations/preview", {"config": ORDER_CONFIG, "payload": order("A-9")}
    ).json()
    assert ready["kind"] == "ready"
    assert ready["output"] == {
        "order_id": "A-9",
        "quantity": 2,
        "customer": {"name": "Ada"},
        "source": "test",
    }
    failed = viewer.post(
        "/api/v1/integrations/preview", {"config": ORDER_CONFIG, "payload": order(qty="many")}
    ).json()
    assert (failed["kind"], failed["stage"], failed["issues"][0]["path"]) == (
        "validation_failed",
        "transform",
        "qty",
    )
    filtered = viewer.post(
        "/api/v1/integrations/preview", {"config": ORDER_CONFIG, "payload": order(status="draft")}
    ).json()
    assert filtered["kind"] == "filtered"
    invalid = viewer.post(
        "/api/v1/integrations/preview", {"config": {"mappings": []}, "payload": {}}
    ).json()
    assert invalid["kind"] == "invalid_config"
    assert harness.events() == []
    assert harness.destination.requests == []


# --- connections ------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"url": "https://169.254.169.254/latest/meta-data/"}, "destination_not_allowed"),
        ({"url": "https://localhost:8443/hook"}, "destination_not_allowed"),
        ({"url": "http://api.example.com/hook"}, "destination_not_allowed"),
        ({"url": "https://10.0.0.8/hook"}, "destination_not_allowed"),
        ({"url": "https://user:pw@api.example.com/hook"}, "destination_not_allowed"),
        ({"url": "https://api.example.com/hook?api_key=abc123"}, "secret_in_url"),
        (
            {"url": "https://api.example.com/hook", "headers": {"X-Api-Key": "abc123"}},
            "secret_in_header",
        ),
        (
            {"url": "https://api.example.com/hook", "headers": {"Host": "internal"}},
            "forbidden_header",
        ),
        (
            {"url": "https://api.example.com/hook", "headers": {"X-Forwarded-For": "1.2.3.4"}},
            "forbidden_header",
        ),
        (
            {"url": "https://api.example.com/hook", "headers": {"X-Note": "a\r\nInjected: 1"}},
            "invalid_header",
        ),
        ({"url": "https://api.example.com/hook", "method": "DELETE"}, "invalid_config"),
        ({"url": "https://api.example.com/hook", "auth": {"type": "bearer"}}, "secret_required"),
        ({"url": "https://api.example.com/hook", "auth": {"type": "header"}}, "invalid_config"),
        ({}, "invalid_config"),
    ],
)
def test_unsafe_destination_configuration_is_rejected_at_save_time(
    harness: Harness, config: dict[str, Any], code: str
) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    response = admin.post(
        "/api/v1/connections",
        {"name": "Unsafe", "direction": "destination", "adapter_type": "http", "config": config},
    )
    assert response.status_code == 422, response.text
    assert code in {d["code"] for d in response.json()["error"]["details"]}
    assert all(c["name"] != "Unsafe" for c in admin.get("/api/v1/connections").json())


@pytest.mark.parametrize(
    "secret_ref",
    [
        "env:RELAY_DATABASE_URL",
        "env:PATH",
        "env:RELAY_INTERNAL_TOKEN",
        "RELAY_SECRET_X",
        "env:relay_secret_x",
        "file:/etc/passwd",
        "a-pasted-secret-value-not-a-reference",
    ],
)
def test_secret_references_cannot_reach_outside_their_namespace(
    harness: Harness, secret_ref: str
) -> None:
    """Otherwise an admin could aim a destination's auth header at Relay's own credentials."""
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    response = admin.post(
        "/api/v1/connections",
        {
            "name": "Leak",
            "direction": "destination",
            "adapter_type": "http",
            "config": {"url": "https://attacker.example/c", "auth": {"type": "bearer"}},
            "secret_ref": secret_ref,
        },
    )
    assert response.status_code == 422
    assert "invalid_secret_ref" in {d["code"] for d in response.json()["error"]["details"]}


def test_connection_lifecycle_and_safe_representation(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    created = admin.post(
        "/api/v1/connections",
        {
            "name": "Billing API",
            "direction": "destination",
            "adapter_type": "http",
            "config": {
                "url": "https://billing.example.com/v1/events",
                "auth": {"type": "header", "header_name": "X-Api-Key"},
            },
            "secret_ref": "env:RELAY_SECRET_BILLING",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["secret_ref"], body["secret_resolvable"], body["webhook_path"]) == (
        "env:RELAY_SECRET_BILLING",
        False,
        None,
    )
    assert (
        admin.post(
            "/api/v1/connections",
            {"name": "Billing API", "direction": "source", "adapter_type": "webhook"},
        ).json()["error"]["code"]
        == "name_taken"
    )
    assert (
        admin.post(
            "/api/v1/connections", {"name": "Odd", "direction": "source", "adapter_type": "kafka"}
        ).json()["error"]["code"]
        == "unknown_adapter"
    )

    unsigned = admin.post(
        "/api/v1/connections",
        {"name": "Open hook", "direction": "source", "adapter_type": "webhook"},
    ).json()
    assert unsigned["webhook_path"] == f"/webhooks/{unsigned['id']}"
    assert "unsigned_webhook" in {w["code"] for w in unsigned["warnings"]}

    url = f"/api/v1/connections/{body['id']}"
    cleared = admin.patch(
        url, {"config": {"url": "https://billing.example.com/v2/events"}, "secret_ref": None}
    ).json()
    assert (cleared["secret_ref"], cleared["secret_resolvable"]) == (None, None)
    assert (
        admin.patch(url, {"name": "Billing v2"}).json()["config"]["url"].endswith("/v2/events")
    )  # omitted fields untouched
    assert admin.delete(url).status_code == 204
    in_use = admin.delete(f"/api/v1/connections/{harness.seeded.destination_id}")  # type: ignore[union-attr]
    assert in_use.json()["error"]["code"] == "connection_in_use"

    audited = [
        (a.action, a.details) for a in harness.audit("connection.") if a.entity_id == body["id"]
    ]
    assert [a for a, _ in audited] == [
        "connection.create",
        "connection.update",
        "connection.update",
        "connection.delete",
    ]
    assert audited[1][1] == {
        "changed": ["config", "secret_ref"],
        "is_active": True,
    }  # field names only
    assert admin.get("/api/v1/adapters").json() == {
        "sources": ["webhook"],
        "destinations": ["http"],
    }


# --- event explorer ---------------------------------------------------------


def make_events(harness: Harness, count: int) -> list[str]:
    ids = []
    for index in range(count):
        harness.clock.advance(1)
        ids.append(
            harness.accepted_event_id(
                harness.send_webhook(order(f"A-{index}"), event_id=f"evt-{index}")
            )
        )
    return ids


def test_keyset_pagination_is_stable_while_new_events_arrive(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    created = make_events(harness, 7)
    first = viewer.get("/api/v1/events", params={"limit": 3}).json()
    assert [e["id"] for e in first["items"]] == created[::-1][:3]
    assert first["next_cursor"]

    # New events arriving between page requests must not shift or repeat rows.
    make_events_after = harness.accepted_event_id(
        harness.send_webhook(order("A-new"), event_id="evt-new")
    )
    second = viewer.get(
        "/api/v1/events", params={"limit": 3, "cursor": first["next_cursor"]}
    ).json()
    third = viewer.get(
        "/api/v1/events", params={"limit": 3, "cursor": second["next_cursor"]}
    ).json()
    seen = [e["id"] for page in (first, second, third) for e in page["items"]]
    assert seen == created[::-1]
    assert make_events_after not in seen
    assert third["next_cursor"] is None

    assert (
        viewer.get("/api/v1/events", params={"cursor": "garbage"}).json()["error"]["code"]
        == "invalid_cursor"
    )
    assert viewer.get("/api/v1/events", params={"limit": 0}).status_code == 422
    assert viewer.get("/api/v1/events", params={"limit": 101}).status_code == 422


def test_events_with_identical_timestamps_paginate_without_loss(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    created = {
        harness.accepted_event_id(harness.send_webhook(order(f"A-{i}"), event_id=f"same-{i}"))
        for i in range(5)
    }
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        page = viewer.get(
            "/api/v1/events", params={"limit": 2, **({"cursor": cursor} if cursor else {})}
        ).json()
        seen += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5
    assert set(seen) == created


def test_event_filters(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    delivered = harness.accepted_event_id(
        harness.send_webhook(order("A-1"), event_id="src-delivered")
    )
    harness.pump()
    harness.clock.advance(60)
    midpoint = harness.clock().isoformat()
    harness.clock.advance(60)
    harness.destination.default.status = 400
    dead = harness.accepted_event_id(harness.send_webhook(order("A-2"), event_id="src-dead"))
    harness.pump()
    invalid = harness.send_webhook({"qty": 1}, event_id="src-invalid").json()["results"][0][
        "event_id"
    ]

    def ids(**params: Any) -> set[str]:
        response = viewer.get("/api/v1/events", params=params)
        assert response.status_code == 200, response.text
        return {e["id"] for e in response.json()["items"]}

    assert ids() == {delivered, dead, invalid}
    assert ids(status="DELIVERED") == {delivered}
    assert ids(status=["DEAD_LETTERED", "VALIDATION_FAILED"]) == {dead, invalid}
    assert ids(received_after=midpoint) == {dead, invalid}
    assert ids(received_before=midpoint) == {delivered}
    assert ids(q="src-dead") == {dead}
    assert ids(q=delivered) == {delivered}
    assert ids(q=harness.event(dead).correlation_id) == {dead}
    assert ids(q="src-") == set()  # exact match only
    assert ids(integration_id=str(harness.seeded.integration_id)) == {delivered, dead, invalid}  # type: ignore[union-attr]
    assert ids(integration_id=str(uuid.uuid4())) == set()
    assert viewer.get("/api/v1/events", params={"status": "EXPLODED"}).status_code == 422
    assert (
        viewer.get("/api/v1/events", params={"received_after": "2026-01-01T00:00:00"}).status_code
        == 422
    )


def test_event_detail_redacts_sensitive_fields_for_every_role(harness: Harness) -> None:
    payload = order("A-1", password="hunter2", notes="card 4111-1111-1111-1111 token=abc123xyz")
    event_id = harness.accepted_event_id(harness.send_webhook(payload, event_id="evt-secret"))
    harness.pump()
    for email in (harness.seeded.admin, harness.seeded.operator, harness.seeded.viewer):  # type: ignore[union-attr]
        detail = harness.login(email).get(f"/api/v1/events/{event_id}")
        assert detail.status_code == 200
        text = detail.text
        assert "4111" not in text
        assert "hunter2" not in text
        assert "abc123xyz" not in text
        body = detail.json()
        assert body["payload"]["payment"]["card_number"] == "[REDACTED]"
        assert body["payload"]["password"] == "[REDACTED]"
        assert body["payload"]["order_no"] == "A-1"
        assert [t["to_status"] for t in body["transitions"]][-1] == "DELIVERED"
    # The stored payload is intact (delivery and replay need it); only display is redacted.
    assert harness.event(event_id).payload["payment"]["card_number"] == "4111 1111 1111 1111"


def test_integration_specific_redaction_fields(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    config = {**ORDER_CONFIG, "redact_fields": ["email", "order_no"]}
    admin.patch(f"/api/v1/integrations/{harness.seeded.integration_id}", {"config": config})  # type: ignore[union-attr]
    event_id = harness.accepted_event_id(harness.send_webhook(order("A-PRIVATE"), event_id="evt-r"))
    body = admin.get(f"/api/v1/events/{event_id}").json()
    assert body["payload"]["customer"]["email"] == "[REDACTED]"
    assert body["payload"]["order_no"] == "[REDACTED]"


# --- dashboard --------------------------------------------------------------


def test_dashboard_figures_are_computed_from_stored_events(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    empty = viewer.get("/api/v1/dashboard/summary").json()
    assert empty["events"]["received"] == 0
    assert empty["events"]["success_rate"] is None  # no outcomes yet: not "100%" and not "0%"
    assert empty["attempts"]["duration_ms_p50"] is None
    assert empty["live"]["oldest_in_flight_age_seconds"] is None
    assert empty["recent_failures"] == []
    assert empty["is_demo_workspace"] is False

    for index in range(3):
        harness.send_webhook(order(f"A-{index}"), event_id=f"ok-{index}")
    harness.pump()
    harness.destination.default.status = 400
    dead = harness.accepted_event_id(harness.send_webhook(order("A-dead"), event_id="dead-1"))
    harness.pump()
    harness.send_webhook({"qty": 1}, event_id="invalid-1")
    harness.destination.default.status = 500
    harness.send_webhook(order("A-retry"), event_id="retry-1")
    harness.pump()
    harness.send_webhook(order(status="draft"), event_id="filtered-1")
    harness.pump()
    harness.clock.advance(30)

    data = viewer.get("/api/v1/dashboard/summary", params={"window": "1h"}).json()
    events = data["events"]
    assert events["received"] == 7
    assert (
        events["delivered"],
        events["failed"],
        events["dead_lettered"],
        events["validation_failed"],
    ) == (3, 2, 1, 1)
    assert (events["filtered"], events["in_flight"]) == (1, 1)
    assert events["success_rate"] == 0.6  # 3 delivered / (3 delivered + 2 failed)
    assert events["by_status"] == {
        "DELIVERED": 3,
        "DEAD_LETTERED": 1,
        "VALIDATION_FAILED": 1,
        "RETRY_SCHEDULED": 1,
        "FILTERED": 1,
    }
    assert data["attempts"]["total"] == 5
    assert data["attempts"]["failed"] == 2
    assert data["attempts"]["duration_ms_p50"] is not None
    assert data["live"]["retry_scheduled"] == 1
    assert data["live"]["oldest_in_flight_age_seconds"] >= 30
    assert {e["id"] for e in data["recent_failures"]} >= {dead}
    (health,) = data["integrations"]
    assert (
        health["received"],
        health["delivered"],
        health["failed"],
        health["in_flight"],
        health["success_rate"],
    ) == (7, 3, 2, 1, 0.6)
    assert sum(p["received"] for p in data["series"]["points"]) == 7
    assert data["series"]["bucket"] == "hour"

    # Outside the window nothing counts, but stuck work is still reported as live.
    harness.clock.advance(2 * 3600)
    later = viewer.get("/api/v1/dashboard/summary", params={"window": "1h"}).json()
    assert later["events"]["received"] == 0
    assert later["live"]["retry_scheduled"] == 1
    assert later["live"]["oldest_pending_outbox_age_seconds"] > 3600
    assert (
        viewer.get("/api/v1/dashboard/summary", params={"window": "7d"}).json()["series"]["bucket"]
        == "day"
    )
    assert viewer.get("/api/v1/dashboard/summary", params={"window": "1y"}).status_code == 422


# --- audit ------------------------------------------------------------------


def test_audit_log_is_filterable_paginated_and_free_of_secrets(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    integration = f"/api/v1/integrations/{harness.seeded.integration_id}"  # type: ignore[union-attr]
    admin.post(f"{integration}/pause")
    admin.post(f"{integration}/resume")
    admin.patch(
        f"/api/v1/connections/{harness.seeded.destination_id}",
        {"secret_ref": "env:RELAY_SECRET_ROTATED"},
    )  # type: ignore[union-attr]
    admin.put("/api/v1/demo/destination", {"mode": "flaky"})

    everything = admin.get("/api/v1/audit-logs").json()["items"]
    assert [e["action"] for e in everything][:4] == [
        "demo.destination_mode",
        "connection.update",
        "integration.resume",
        "integration.pause",
    ]
    assert all(e["actor_email"] == harness.seeded.admin for e in everything[:4])  # type: ignore[union-attr]
    assert all(e["correlation_id"] for e in everything[:4])
    assert "RELAY_SECRET_ROTATED" not in str(everything)

    only = admin.get("/api/v1/audit-logs", params={"action": "integration."}).json()["items"]
    assert {e["action"] for e in only} <= {
        "integration.create",
        "integration.activate",
        "integration.pause",
        "integration.resume",
    }
    by_entity = admin.get(
        "/api/v1/audit-logs",
        params={"entity_type": "integration", "entity_id": str(harness.seeded.integration_id)},
    ).json()["items"]  # type: ignore[union-attr]
    assert len(by_entity) == len(only)
    page = admin.get("/api/v1/audit-logs", params={"limit": 2}).json()
    rest = admin.get(
        "/api/v1/audit-logs", params={"limit": 50, "cursor": page["next_cursor"]}
    ).json()
    assert [e["id"] for e in page["items"] + rest["items"]] == [e["id"] for e in everything]


# --- API conventions --------------------------------------------------------


def test_errors_share_one_envelope_and_never_leak_internals(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    samples = [
        viewer.get(f"/api/v1/events/{uuid.uuid4()}"),
        viewer.get("/api/v1/events/not-a-uuid"),
        viewer.get("/api/v1/no-such-route"),
        viewer.http.put("/api/v1/events"),
        viewer.post("/api/v1/integrations/preview", {"config": {}, "payload": {}, "extra": 1}),
    ]
    assert [r.status_code for r in samples] == [404, 422, 404, 405, 422]
    for response in samples:
        error = response.json()["error"]
        assert set(error) == {"code", "message", "details", "correlation_id"}
        assert error["correlation_id"] == response.headers["x-correlation-id"]
        for leak in ("Traceback", "sqlalchemy", "psycopg", "SELECT ", "relay_core"):
            assert leak not in response.text
    # Validation errors locate the problem but never echo the rejected input.
    echoed = viewer.post(
        "/api/v1/integrations/preview", {"config": "SENSITIVE-INPUT-VALUE", "payload": {}}
    )
    assert echoed.status_code == 422
    assert "SENSITIVE-INPUT-VALUE" not in echoed.text
    assert echoed.json()["error"]["details"][0]["path"] == "config"


def test_unexpected_errors_return_a_generic_500(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relay_core.services import queries

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "connection to server at 10.0.0.5 failed: password authentication failed for user relay_prod"
        )

    monkeypatch.setattr(queries, "list_events", boom)
    client = TestClient(harness.api_app, raise_server_exceptions=False)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": harness.seeded.viewer, "password": "correct horse battery staple"},
    )  # type: ignore[union-attr]
    assert login.status_code == 200
    response = client.get("/api/v1/events")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["message"] == "an unexpected error occurred"
    assert "10.0.0.5" not in response.text
    assert "relay_prod" not in response.text


def test_security_headers_and_health_endpoints(harness: Harness) -> None:
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    response = viewer.get("/api/v1/events")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    for client in (harness.api, harness.worker):
        assert client.get("/healthz").json() == {"status": "ok"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "checks": {"database": "ok", "migrations": "ok"}}


def test_openapi_document_describes_the_contract(harness: Harness) -> None:
    spec = harness.api.get("/openapi.json").json()
    paths = spec["paths"]
    for path in (
        "/api/v1/auth/login", "/api/v1/integrations", "/api/v1/integrations/{integration_id}/validate",
        "/webhooks/{connection_id}", "/api/v1/events", "/api/v1/events/{event_id}/attempts",
        "/api/v1/events/{event_id}/replay", "/api/v1/events/{event_id}/investigations",
        "/api/v1/investigations/{investigation_id}", "/api/v1/dashboard/summary", "/healthz", "/readyz",
    ):  # fmt: skip
        assert path in paths, path
    replay = paths["/api/v1/events/{event_id}/replay"]["post"]
    assert "Idempotency-Key" in {p["name"] for p in replay["parameters"]}
    assert "ErrorResponse" in spec["components"]["schemas"]
    # Internal worker endpoints are not part of the public API.
    assert not any(path.startswith("/internal") for path in paths)
    assert harness.worker.get("/openapi.json").status_code == 404
