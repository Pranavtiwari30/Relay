"""Authentication, CSRF, role enforcement and workspace isolation — exercised by calling
the API directly, never through the UI."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from relay_core.db.models import UserSession
from tests.support import ORIGIN, PASSWORD, Harness, UserClient, order

NEW_PASSWORD = "a brand new passphrase 42"


def dead_lettered_event(harness: Harness) -> str:
    harness.destination.default.status = 400
    event_id = harness.accepted_event_id(
        harness.send_webhook(order(), event_id=f"evt-{uuid.uuid4().hex[:8]}")
    )
    harness.pump()
    harness.destination.reset()
    assert harness.event(event_id).status == "DEAD_LETTERED"
    return event_id


# --- authentication ---------------------------------------------------------


def test_login_sets_a_hardened_cookie_and_returns_session_details(harness: Harness) -> None:
    assert harness.seeded is not None
    client = TestClient(harness.api_app)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": harness.seeded.operator.upper(), "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "relay_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    body = response.json()
    assert body["user"]["email"] == harness.seeded.operator
    assert body["role"] == "operator"
    assert "event:replay" in body["permissions"]
    assert "integration:write" not in body["permissions"]
    assert len(body["csrf_token"]) >= 32
    assert "password" not in response.text.lower()

    # Only a hash of the token is stored server-side.
    token = client.cookies["relay_session"]
    with harness.session_factory() as session:
        stored = session.execute(select(UserSession)).scalars().one()
    assert stored.token_hash != token
    assert token not in (stored.token_hash, stored.csrf_token)


@pytest.mark.parametrize(
    ("email", "password"),
    [("operator@acme.test", "wrong password here"), ("nobody@acme.test", PASSWORD), ("", PASSWORD)],
)
def test_bad_credentials_give_one_indistinguishable_error(
    harness: Harness, email: str, password: str
) -> None:
    response = harness.api.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code in (401, 422)
    if response.status_code == 401:
        assert response.json()["error"] == {
            "code": "invalid_credentials",
            "message": "invalid email or password",
            "details": [],
            "correlation_id": response.headers["x-correlation-id"],
        }
    assert "set-cookie" not in response.headers


def test_login_attempts_are_rate_limited_per_account(
    session_factory, database_url, destination
) -> None:  # type: ignore[no-untyped-def]
    harness = Harness(session_factory, database_url, destination, login_rate_limit_per_15_minutes=3)
    seeded = harness.seed()
    codes = [
        harness.api.post(
            "/api/v1/auth/login", json={"email": seeded.admin, "password": f"guess-number-{i}"}
        ).status_code
        for i in range(5)
    ]
    assert codes == [401, 401, 401, 429, 429]
    # Even the right password is refused while the account is locked out...
    locked = harness.api.post(
        "/api/v1/auth/login", json={"email": seeded.admin, "password": PASSWORD}
    )
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    # ...other accounts are unaffected, and the window expires.
    assert (
        harness.api.post(
            "/api/v1/auth/login", json={"email": seeded.viewer, "password": PASSWORD}
        ).status_code
        == 200
    )
    harness.clock.advance(901)
    assert (
        harness.api.post(
            "/api/v1/auth/login", json={"email": seeded.admin, "password": PASSWORD}
        ).status_code
        == 200
    )


def test_unauthenticated_requests_are_rejected_everywhere(harness: Harness) -> None:
    anonymous = TestClient(harness.api_app)
    paths = [
        "/api/v1/auth/me", "/api/v1/workspaces", "/api/v1/members", "/api/v1/connections", "/api/v1/integrations",
        "/api/v1/events", "/api/v1/investigations", "/api/v1/dashboard/summary", "/api/v1/audit-logs",
        "/api/v1/demo/destination", "/api/v1/adapters", "/api/v1/ai/status",
    ]  # fmt: skip
    for path in paths:
        response = anonymous.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthenticated"
    forged = TestClient(harness.api_app, cookies={"relay_session": "x" * 43})
    assert forged.get("/api/v1/events").status_code == 401


def test_logout_revokes_the_session_server_side(harness: Harness) -> None:
    client = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    token = client.http.cookies["relay_session"]
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 204
    # Replaying the old cookie must not work: revocation is server-side.
    replayed = TestClient(harness.api_app, cookies={"relay_session": token})
    assert replayed.get("/api/v1/auth/me").status_code == 401


def test_sessions_expire(harness: Harness) -> None:
    client = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    harness.clock.advance(harness.settings.session_ttl_hours * 3600 + 1)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_password_change_requires_the_old_password_and_revokes_other_sessions(
    harness: Harness,
) -> None:
    email = harness.seeded.operator  # type: ignore[union-attr]
    here, elsewhere = harness.login(email), harness.login(email)
    wrong = here.post(
        "/api/v1/auth/password",
        {"current_password": "not it at all!", "new_password": NEW_PASSWORD},
    )
    assert wrong.status_code == 401
    weak = here.post(
        "/api/v1/auth/password", {"current_password": PASSWORD, "new_password": "short"}
    )
    assert weak.status_code == 422
    assert (
        here.post(
            "/api/v1/auth/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    assert here.get("/api/v1/auth/me").status_code == 200
    assert elsewhere.get("/api/v1/auth/me").status_code == 401
    assert (
        harness.api.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        harness.api.post(
            "/api/v1/auth/login", json={"email": email, "password": NEW_PASSWORD}
        ).status_code
        == 200
    )


# --- CSRF and origin --------------------------------------------------------


def test_state_changing_requests_need_the_csrf_token(harness: Harness) -> None:
    client = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    target = f"/api/v1/integrations/{harness.seeded.integration_id}/pause"  # type: ignore[union-attr]
    # A cross-site form post carries the cookie automatically but cannot set this header.
    missing = client.http.post(target)
    wrong = client.http.post(target, headers={"X-CSRF-Token": "guessed-value"})
    other_session = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    stolen = client.http.post(target, headers={"X-CSRF-Token": other_session.csrf})
    for response in (missing, wrong, stolen):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_failed"
    assert harness.integration().status == "active"
    assert client.post(target).status_code == 200
    assert harness.integration().status == "paused"


def test_requests_from_unlisted_origins_are_refused(harness: Harness) -> None:
    client = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    target = f"/api/v1/integrations/{harness.seeded.integration_id}/pause"  # type: ignore[union-attr]
    evil = client.http.post(
        target, headers={"X-CSRF-Token": client.csrf, "Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    assert evil.json()["error"]["code"] == "origin_not_allowed"
    login = harness.api.post(
        "/api/v1/auth/login",
        json={"email": harness.seeded.admin, "password": PASSWORD},
        headers={"Origin": "https://evil.example"},  # type: ignore[union-attr]
    )
    assert login.status_code == 403
    assert harness.integration().status == "active"


def test_cors_allows_only_configured_origins(harness: Harness) -> None:
    def preflight(origin: str) -> Any:
        return harness.api.options(
            "/api/v1/events",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-csrf-token",
            },
        )

    allowed = preflight(ORIGIN)
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    denied = preflight("https://evil.example")
    assert "access-control-allow-origin" not in denied.headers


# --- role enforcement (acceptance 8) ---------------------------------------


def restricted_calls(harness: Harness, event_id: str) -> list[tuple[str, str, Any, str]]:
    """(method, path, body, minimum role) for every state-changing endpoint."""
    s = harness.seeded
    assert s is not None
    integration = f"/api/v1/integrations/{s.integration_id}"
    return [
        (
            "post",
            "/api/v1/connections",
            {"name": "X", "direction": "source", "adapter_type": "webhook", "config": {}},
            "admin",
        ),
        ("patch", f"/api/v1/connections/{s.source_id}", {"name": "Renamed"}, "admin"),
        ("delete", f"/api/v1/connections/{s.source_id}", None, "admin"),
        (
            "post",
            "/api/v1/integrations",
            {
                "name": "New",
                "source_connection_id": str(s.source_id),
                "destination_connection_id": str(s.destination_id),
                "config": {"passthrough": True},
            },
            "admin",
        ),
        ("patch", integration, {"description": "changed"}, "admin"),
        ("post", f"{integration}/disable", None, "admin"),
        ("post", f"{integration}/activate", None, "admin"),
        (
            "post",
            "/api/v1/members",
            {"email": "new@acme.test", "role": "admin", "password": "another long passphrase"},
            "admin",
        ),
        ("patch", f"/api/v1/members/{uuid.uuid4()}", {"role": "admin"}, "admin"),
        ("delete", f"/api/v1/members/{uuid.uuid4()}", None, "admin"),
        ("post", f"{integration}/pause", None, "operator"),
        ("post", f"{integration}/resume", None, "operator"),
        ("post", f"/api/v1/events/{event_id}/replay", {"reason": "please"}, "operator"),
        ("post", f"/api/v1/events/{event_id}/investigations", None, "operator"),
        (
            "post",
            f"/api/v1/investigations/{uuid.uuid4()}/recommendations/rec-1/decision",
            {"decision": "approved"},
            "operator",
        ),
        ("put", "/api/v1/demo/destination", {"mode": "error_500"}, "operator"),
        (
            "post",
            "/api/v1/simulator/send",
            {"connection_id": str(s.source_id), "scenario": "valid_order"},
            "operator",
        ),
    ]


def call(client: UserClient, method: str, path: str, body: Any) -> Any:
    if method == "delete":
        return client.delete(path)
    return getattr(client, method)(
        path, body, headers={"Idempotency-Key": f"rbac-{uuid.uuid4().hex}"}
    )


def test_viewer_cannot_perform_any_restricted_action(harness: Harness) -> None:
    event_id = dead_lettered_event(harness)
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    before = (len(harness.audit()), harness.integration().status, harness.event(event_id).status)
    for method, path, body, _ in restricted_calls(harness, event_id):
        response = call(viewer, method, path, body)
        assert response.status_code == 403, f"{method.upper()} {path} -> {response.status_code}"
        error = response.json()["error"]
        assert error["code"] == "permission_denied"
        assert "viewer" in error["message"]
    # Nothing changed and nothing was audited as done.
    assert (
        len(harness.audit()),
        harness.integration().status,
        harness.event(event_id).status,
    ) == before
    assert harness.destination.requests == []


def test_operator_can_operate_but_not_administer(harness: Harness) -> None:
    event_id = dead_lettered_event(harness)
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    for method, path, body, minimum in restricted_calls(harness, event_id):
        response = call(operator, method, path, body)
        if minimum == "admin":
            assert response.status_code == 403, f"{method.upper()} {path} -> {response.status_code}"
        else:
            assert response.status_code != 403, f"{method.upper()} {path} unexpectedly forbidden"


def test_viewer_can_read(harness: Harness) -> None:
    event_id = dead_lettered_event(harness)
    viewer = harness.login(harness.seeded.viewer)  # type: ignore[union-attr]
    for path in (
        "/api/v1/events", f"/api/v1/events/{event_id}", f"/api/v1/events/{event_id}/attempts", "/api/v1/integrations",
        f"/api/v1/integrations/{harness.seeded.integration_id}", "/api/v1/connections", "/api/v1/dashboard/summary",  # type: ignore[union-attr]
        "/api/v1/audit-logs", "/api/v1/members", "/api/v1/investigations",
    ):  # fmt: skip
        assert viewer.get(path).status_code == 200, path


def test_role_is_read_from_the_database_on_every_request(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    operator = harness.login(harness.seeded.operator)  # type: ignore[union-attr]
    target = f"/api/v1/integrations/{harness.seeded.integration_id}/pause"  # type: ignore[union-attr]
    members = {m["email"]: m["user_id"] for m in admin.get("/api/v1/members").json()}
    assert (
        admin.patch(
            f"/api/v1/members/{members[harness.seeded.operator]}", {"role": "viewer"}
        ).status_code
        == 200
    )  # type: ignore[union-attr]
    # The operator's existing session loses the permission immediately.
    assert operator.post(target).status_code == 403
    assert admin.delete(f"/api/v1/members/{members[harness.seeded.operator]}").status_code == 204  # type: ignore[union-attr]
    assert operator.get("/api/v1/events").status_code == 403
    assert operator.get("/api/v1/events").json()["error"]["code"] == "no_workspace"


def test_member_management_rules(harness: Harness) -> None:
    admin = harness.login(harness.seeded.admin)  # type: ignore[union-attr]
    members = {m["email"]: m["user_id"] for m in admin.get("/api/v1/members").json()}
    me = members[harness.seeded.admin]  # type: ignore[union-attr]
    # The last administrator can be neither demoted nor removed.
    assert (
        admin.patch(f"/api/v1/members/{me}", {"role": "viewer"}).json()["error"]["code"]
        == "last_admin"
    )
    assert admin.delete(f"/api/v1/members/{me}").json()["error"]["code"] == "last_admin"

    created = admin.post(
        "/api/v1/members",
        {
            "email": "New.Person@Acme.test",
            "display_name": "New",
            "role": "operator",
            "password": "a sufficiently long one",
        },
    )
    assert created.status_code == 201
    assert created.json()["email"] == "new.person@acme.test"
    assert (
        admin.post("/api/v1/members", {"email": "new.person@acme.test", "role": "viewer"}).json()[
            "error"
        ]["code"]
        == "already_member"
    )
    assert (
        admin.post("/api/v1/members", {"email": "nopass@acme.test", "role": "viewer"}).json()[
            "error"
        ]["code"]
        == "password_required"
    )
    assert (
        admin.post(
            "/api/v1/members",
            {"email": "x@acme.test", "role": "owner", "password": "a sufficiently long one"},
        ).status_code
        == 422
    )
    assert (
        harness.login("new.person@acme.test", "a sufficiently long one")
        .get("/api/v1/auth/me")
        .json()["role"]
        == "operator"
    )

    actions = [(a.action, a.details) for a in harness.audit("member.")]
    assert actions == [
        ("member.add", {"email": "new.person@acme.test", "role": "operator", "created_user": True})
    ]
    assert "sufficiently" not in str([a.details for a in harness.audit()])


# --- workspace isolation ----------------------------------------------------


def test_another_workspace_cannot_see_or_touch_anything(harness: Harness) -> None:
    ours = harness.seeded
    assert ours is not None
    event_id = dead_lettered_event(harness)
    investigation = (
        harness.login(ours.operator).post(f"/api/v1/events/{event_id}/investigations").json()["id"]
    )
    theirs = harness.seed(slug="rival")
    intruder = harness.login(theirs.admin)  # an administrator — of a different workspace

    for path in (
        "/api/v1/events",
        "/api/v1/integrations",
        "/api/v1/connections",
        "/api/v1/audit-logs",
        "/api/v1/investigations",
    ):
        body = intruder.get(path).json()
        items = body["items"] if isinstance(body, dict) else body
        assert all(str(ours.workspace_id) not in str(item) for item in items)
        assert event_id not in str(body)
    assert len(intruder.get("/api/v1/integrations").json()) == 1  # only their own
    assert intruder.get("/api/v1/dashboard/summary").json()["events"]["received"] == 0

    reads = [
        f"/api/v1/events/{event_id}", f"/api/v1/events/{event_id}/attempts", f"/api/v1/integrations/{ours.integration_id}",
        f"/api/v1/integrations/{ours.integration_id}/versions", f"/api/v1/connections/{ours.source_id}",
        f"/api/v1/investigations/{investigation}",
    ]  # fmt: skip
    for path in reads:
        response = intruder.get(path)
        assert response.status_code == 404, path
        assert event_id not in response.text or "not found" in response.text

    writes = [
        ("post", f"/api/v1/events/{event_id}/replay", {"reason": "steal"}),
        ("post", f"/api/v1/events/{event_id}/investigations", None),
        ("post", f"/api/v1/integrations/{ours.integration_id}/disable", None),
        ("patch", f"/api/v1/integrations/{ours.integration_id}", {"description": "pwned"}),
        (
            "patch",
            f"/api/v1/connections/{ours.destination_id}",
            {"config": {"url": "https://evil.example/collect"}},
        ),
        ("delete", f"/api/v1/connections/{ours.source_id}", None),
        (
            "post",
            f"/api/v1/investigations/{investigation}/recommendations/rec-1/decision",
            {"decision": "approved"},
        ),
        (
            "post",
            "/api/v1/simulator/send",
            {"connection_id": str(ours.source_id), "scenario": "valid_order"},
        ),
    ]
    for method, path, body in writes:
        assert call(intruder, method, path, body).status_code == 404, path

    # They cannot wire their own integration to our connections either.
    cross = intruder.post(
        "/api/v1/integrations",
        {
            "name": "Leech",
            "source_connection_id": str(ours.source_id),
            "destination_connection_id": str(theirs.destination_id),
            "config": {"passthrough": True},
        },
    )
    assert cross.status_code == 422

    assert harness.event(event_id).status == "DEAD_LETTERED"
    assert harness.integration(ours.integration_id).status == "active"
    assert harness.integration(ours.integration_id).description == ""


def test_workspace_header_cannot_select_a_workspace_you_do_not_belong_to(harness: Harness) -> None:
    ours = harness.seeded
    assert ours is not None
    theirs = harness.seed(slug="rival")
    intruder = harness.login(theirs.admin, workspace_id=ours.workspace_id)
    response = intruder.get("/api/v1/events")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "workspace not found"
    # Indistinguishable from a workspace that does not exist at all.
    nowhere = harness.login(theirs.admin, workspace_id=uuid.uuid4())
    missing = nowhere.get("/api/v1/events")
    assert missing.status_code == 404
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "correlation_id"}  # noqa: E731
    assert strip(missing) == strip(response)
    garbage = harness.login(theirs.admin)
    assert (
        garbage.get("/api/v1/events", headers={"X-Workspace-Id": "not-a-uuid"}).status_code == 404
    )


def test_a_user_in_two_workspaces_acts_with_the_role_of_the_selected_one(harness: Harness) -> None:
    ours = harness.seeded
    assert ours is not None
    theirs = harness.seed(slug="rival")
    harness.add_user(ours.viewer, "admin", theirs.workspace_id)  # viewer here, admin there
    in_ours = harness.login(ours.viewer, workspace_id=ours.workspace_id)
    in_theirs = harness.login(ours.viewer, workspace_id=theirs.workspace_id)
    assert in_ours.post(f"/api/v1/integrations/{ours.integration_id}/pause").status_code == 403
    assert in_theirs.post(f"/api/v1/integrations/{theirs.integration_id}/pause").status_code == 200
    # Admin rights in one workspace do not reach into the other.
    assert in_theirs.post(f"/api/v1/integrations/{ours.integration_id}/pause").status_code == 404
    assert {w["role"] for w in in_ours.get("/api/v1/auth/me").json()["workspaces"]} == {
        "viewer",
        "admin",
    }
