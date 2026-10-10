"""State machine, retry policy, pagination, roles and configuration validation."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from relay_core.config import Settings
from relay_core.domain.pagination import clamp_limit, decode_cursor, encode_cursor
from relay_core.domain.retry import (
    Disposition,
    FailureCategory,
    RetryPolicy,
    classify_http_status,
    compute_backoff,
    decide_retry,
)
from relay_core.domain.roles import ROLE_PERMISSIONS, Permission, Role, has_permission
from relay_core.domain.states import (
    CLAIMABLE,
    REPLAYABLE,
    TERMINAL,
    TRANSITIONS,
    EventStatus,
    can_transition,
)
from relay_core.errors import BadRequestError

S = EventStatus

# --- finite-state machine --------------------------------------------------

# The complete set of legal edges, written out independently of the table so a
# careless edit to TRANSITIONS fails this test instead of silently widening it.
EXPECTED_EDGES = {
    (S.RECEIVED, S.VALIDATION_FAILED),
    (S.RECEIVED, S.PENDING_PUBLISH),
    (S.RECEIVED, S.HELD),
    (S.HELD, S.PENDING_PUBLISH),
    (S.HELD, S.DEAD_LETTERED),
    (S.PENDING_PUBLISH, S.QUEUED),
    (S.PENDING_PUBLISH, S.PROCESSING),
    (S.PENDING_PUBLISH, S.HELD),
    (S.PENDING_PUBLISH, S.DEAD_LETTERED),
    (S.QUEUED, S.PROCESSING),
    (S.QUEUED, S.HELD),
    (S.QUEUED, S.DEAD_LETTERED),
    (S.PROCESSING, S.DELIVERED),
    (S.PROCESSING, S.RETRY_SCHEDULED),
    (S.PROCESSING, S.DEAD_LETTERED),
    (S.PROCESSING, S.VALIDATION_FAILED),
    (S.PROCESSING, S.FILTERED),
    (S.RETRY_SCHEDULED, S.QUEUED),
    (S.RETRY_SCHEDULED, S.PROCESSING),
    (S.RETRY_SCHEDULED, S.HELD),
    (S.RETRY_SCHEDULED, S.DEAD_LETTERED),
    (S.VALIDATION_FAILED, S.REPLAY_REQUESTED),
    (S.DEAD_LETTERED, S.REPLAY_REQUESTED),
    (S.REPLAY_REQUESTED, S.PENDING_PUBLISH),
    (S.REPLAY_REQUESTED, S.HELD),
}


@pytest.mark.parametrize("current", list(S))
@pytest.mark.parametrize("target", list(S))
def test_every_transition_pair_is_explicitly_allowed_or_forbidden(
    current: EventStatus, target: EventStatus
) -> None:
    assert can_transition(current, target) is ((current, target) in EXPECTED_EDGES)


def test_every_status_has_a_row_and_no_self_loops() -> None:
    assert set(TRANSITIONS) == set(S)
    assert all(status not in targets for status, targets in TRANSITIONS.items())


def test_delivered_and_filtered_are_final() -> None:
    assert TRANSITIONS[S.DELIVERED] == frozenset()
    assert TRANSITIONS[S.FILTERED] == frozenset()
    assert S.DELIVERED not in REPLAYABLE


def test_terminal_states_only_leave_through_operator_replay() -> None:
    for status in TERMINAL:
        assert TRANSITIONS[status] <= {S.REPLAY_REQUESTED}
    assert {s for s in TERMINAL if TRANSITIONS[s]} == REPLAYABLE


def test_every_non_terminal_state_can_reach_a_terminal_state() -> None:
    def reachable(start: EventStatus) -> set[EventStatus]:
        seen, stack = set(), [start]
        while stack:
            for nxt in TRANSITIONS[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    for status in S:
        if status not in TERMINAL:
            assert reachable(status) & TERMINAL, f"{status} can never finish"


def test_only_claimable_states_lead_to_processing() -> None:
    assert {s for s in S if S.PROCESSING in TRANSITIONS[s]} == CLAIMABLE


# --- retry classification and backoff --------------------------------------


@pytest.mark.parametrize(
    ("status", "disposition", "category"),
    [
        (200, Disposition.SUCCESS, None),
        (204, Disposition.SUCCESS, None),
        (301, Disposition.PERMANENT, FailureCategory.REDIRECT),
        (400, Disposition.PERMANENT, FailureCategory.HTTP_4XX),
        (401, Disposition.PERMANENT, FailureCategory.AUTH),
        (403, Disposition.PERMANENT, FailureCategory.AUTH),
        (404, Disposition.PERMANENT, FailureCategory.HTTP_4XX),
        (408, Disposition.RETRYABLE, FailureCategory.HTTP_408),
        (409, Disposition.PERMANENT, FailureCategory.HTTP_4XX),
        (422, Disposition.PERMANENT, FailureCategory.HTTP_4XX),
        (429, Disposition.RETRYABLE, FailureCategory.HTTP_429),
        (500, Disposition.RETRYABLE, FailureCategory.HTTP_5XX),
        (503, Disposition.RETRYABLE, FailureCategory.HTTP_5XX),
        (99, Disposition.PERMANENT, FailureCategory.INVALID_RESPONSE),
        (600, Disposition.PERMANENT, FailureCategory.INVALID_RESPONSE),
    ],
)
def test_classify_http_status(
    status: int, disposition: Disposition, category: FailureCategory | None
) -> None:
    assert classify_http_status(status) == (disposition, category)


@given(
    attempt=st.integers(1, 200),
    base=st.floats(0.1, 3600),
    cap=st.floats(1, 86_400),
    seed=st.integers(),
    retry_after=st.one_of(st.none(), st.floats(0, 1e9)),
)
def test_backoff_is_always_bounded(
    attempt: int, base: float, cap: float, seed: int, retry_after: float | None
) -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_seconds=base, max_delay_seconds=cap)
    delay = compute_backoff(attempt, policy, random.Random(seed), retry_after)
    assert 0 <= delay <= cap + 0.001


def test_backoff_ceiling_doubles_then_saturates() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_seconds=2, max_delay_seconds=30)

    class Max(random.Random):
        def uniform(self, a: float, b: float) -> float:
            return b

    assert [compute_backoff(n, policy, Max()) for n in range(1, 7)] == [2, 4, 8, 16, 30, 30]


def test_backoff_is_deterministic_for_a_seeded_rng() -> None:
    policy = RetryPolicy()
    assert compute_backoff(3, policy, random.Random(7)) == compute_backoff(
        3, policy, random.Random(7)
    )


def test_retry_after_raises_delay_but_is_capped() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=1, max_delay_seconds=60)
    assert compute_backoff(1, policy, random.Random(1), retry_after_seconds=20) == 20
    assert compute_backoff(1, policy, random.Random(1), retry_after_seconds=86_400) == 60


def test_backoff_rejects_non_positive_attempt_numbers() -> None:
    with pytest.raises(ValueError, match="1-based"):
        compute_backoff(0, RetryPolicy(), random.Random())


def test_decide_retry_respects_disposition_and_attempt_ceiling() -> None:
    policy, rng = RetryPolicy(max_attempts=3), random.Random(0)

    def decide(disposition: Disposition, attempt: int) -> tuple[bool, str]:
        d = decide_retry(disposition=disposition, attempt_number=attempt, policy=policy, rng=rng)
        return d.retry, d.reason

    assert decide(Disposition.SUCCESS, 1) == (False, "delivered")
    assert decide(Disposition.PERMANENT, 1) == (False, "permanent_failure")
    assert decide(Disposition.RETRYABLE, 1) == (True, "retry_scheduled")
    assert decide(Disposition.RETRYABLE, 2) == (True, "retry_scheduled")
    assert decide(Disposition.RETRYABLE, 3) == (False, "attempts_exhausted")
    assert decide(Disposition.RETRYABLE, 9) == (False, "attempts_exhausted")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"base_delay_seconds": 0},
        {"max_delay_seconds": 10**6},
    ],
)
def test_retry_policy_bounds_are_enforced(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


# --- pagination ------------------------------------------------------------


@given(
    moment=st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
    ),
    row_id=st.uuids(),
)
def test_cursor_round_trip(moment: datetime, row_id: UUID) -> None:
    assert decode_cursor(encode_cursor(moment, row_id)) == (moment, row_id)


@pytest.mark.parametrize("cursor", ["", "!!!", "bm90LWpzb24", "WzEsMl0", "x" * 300])
def test_malformed_cursor_is_a_client_error(cursor: str) -> None:
    with pytest.raises(BadRequestError) as info:
        decode_cursor(cursor)
    assert info.value.code == "invalid_cursor"


def test_naive_cursor_timestamp_is_rejected() -> None:
    import base64
    import json

    raw = base64.urlsafe_b64encode(
        json.dumps(["2026-01-01T00:00:00", str(uuid4())]).encode()
    ).decode()
    with pytest.raises(BadRequestError):
        decode_cursor(raw)


def test_clamp_limit() -> None:
    assert clamp_limit(None) == 25
    assert clamp_limit(0) == 1
    assert clamp_limit(10_000) == 100


# --- roles -----------------------------------------------------------------


def test_roles_form_a_strict_hierarchy() -> None:
    viewer, operator, admin = (
        ROLE_PERMISSIONS[r] for r in (Role.VIEWER, Role.OPERATOR, Role.ADMIN)
    )
    assert viewer < operator < admin
    assert admin == frozenset(Permission)


def test_viewer_has_no_mutating_permission() -> None:
    mutating = {
        Permission.MEMBER_MANAGE,
        Permission.CONNECTION_WRITE,
        Permission.INTEGRATION_WRITE,
        Permission.INTEGRATION_OPERATE,
        Permission.EVENT_REPLAY,
        Permission.INVESTIGATION_RUN,
        Permission.RECOMMENDATION_DECIDE,
        Permission.DEMO_USE,
    }
    assert not mutating & ROLE_PERMISSIONS[Role.VIEWER]
    assert has_permission(Role.OPERATOR, Permission.EVENT_REPLAY)
    assert not has_permission(Role.OPERATOR, Permission.INTEGRATION_WRITE)


# --- configuration ---------------------------------------------------------

CLOUD = {
    "env": "cloud",
    "session_cookie_secure": True,
    "queue_backend": "pubsub",
    "pubsub_project_id": "p",
    "internal_auth_mode": "oidc",
    "internal_oidc_audience": "https://worker.example",
}


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_local_defaults_are_valid_and_need_no_cloud_or_model() -> None:
    cfg = settings()
    assert cfg.env == "local"
    assert cfg.queue_backend == "local"
    assert cfg.ai_providers == []
    assert cfg.destination_allow_private_hosts == []


def test_csv_settings_are_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("RELAY_AI_PROVIDERS", "ollama,anthropic")
    cfg = settings()
    assert cfg.cors_allowed_origins == ["https://a.example", "https://b.example"]
    assert cfg.ai_providers == ["ollama", "anthropic"]


def test_valid_cloud_profile() -> None:
    assert settings(**CLOUD).env == "cloud"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"cors_allowed_origins": ["*"]}, "explicit allowlist"),
        ({"session_cookie_samesite": "none"}, "SameSite=None"),
        ({"internal_auth_mode": "oidc"}, "OIDC_AUDIENCE"),
        ({"queue_backend": "pubsub"}, "PUBSUB_PROJECT_ID"),
        ({"ai_providers": ["skynet"]}, "unknown AI provider"),
        ({**CLOUD, "session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
        ({**CLOUD, "destination_allow_http": True}, "SSRF dev overrides"),
        ({**CLOUD, "destination_allow_private_hosts": ["localhost:9000"]}, "SSRF dev overrides"),
        ({**CLOUD, "queue_backend": "local"}, "QUEUE_BACKEND=pubsub"),
        ({**CLOUD, "background_loops": True}, "BACKGROUND_LOOPS"),
        (
            {**CLOUD, "internal_auth_mode": "token", "internal_oidc_audience": None},
            "internal authentication",
        ),
    ],
)
def test_unsafe_configuration_fails_at_startup(overrides: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        settings(**overrides)
