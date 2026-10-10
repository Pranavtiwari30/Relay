"""Delivery outcome classification and bounded exponential backoff.

Application-level retries (this module) cover *destination* outcomes only.
Broker redelivery covers worker/infrastructure failures. Keeping the two apart
avoids multiplying retries — see ``docs/architecture/reliability.md``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field


class Disposition(StrEnum):
    SUCCESS = "success"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


class FailureCategory(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_RESPONSE = "invalid_response"
    HTTP_408 = "http_408"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    HTTP_4XX = "http_4xx"
    AUTH = "auth"
    REDIRECT = "redirect"
    BLOCKED_DESTINATION = "blocked_destination"
    CONFIGURATION = "configuration"
    WORKER_INTERRUPTED = "worker_interrupted"
    QUEUE_EXHAUSTED = "queue_exhausted"
    INTEGRATION_DISABLED = "integration_disabled"


class RetryPolicy(BaseModel):
    """Per-integration retry settings. All bounds are hard limits."""

    max_attempts: int = Field(default=5, ge=1, le=10)
    base_delay_seconds: float = Field(default=2.0, ge=0.1, le=3600)
    max_delay_seconds: float = Field(default=300.0, ge=1, le=86_400)


def classify_http_status(status: int) -> tuple[Disposition, FailureCategory | None]:
    if 200 <= status < 300:
        return Disposition.SUCCESS, None
    if status == 408:
        return Disposition.RETRYABLE, FailureCategory.HTTP_408
    if status in (425, 429):
        return Disposition.RETRYABLE, FailureCategory.HTTP_429
    if status in (401, 403, 407):
        return Disposition.PERMANENT, FailureCategory.AUTH
    if 300 <= status < 400:
        # Redirects are never followed (SSRF defence), so a 3xx cannot succeed on retry.
        return Disposition.PERMANENT, FailureCategory.REDIRECT
    if 400 <= status < 500:
        return Disposition.PERMANENT, FailureCategory.HTTP_4XX
    if 500 <= status < 600:
        return Disposition.RETRYABLE, FailureCategory.HTTP_5XX
    return Disposition.PERMANENT, FailureCategory.INVALID_RESPONSE


def compute_backoff(
    attempt_number: int,
    policy: RetryPolicy,
    rng: random.Random,
    retry_after_seconds: float | None = None,
) -> float:
    """Delay before the attempt that follows ``attempt_number`` (1-based).

    Full jitter: uniform in ``[0, min(max_delay, base * 2**(attempt-1))]``. A
    destination-supplied ``Retry-After`` raises the delay but is itself capped
    at ``max_delay`` so a hostile or broken destination cannot park an event.
    """
    if attempt_number < 1:
        raise ValueError("attempt_number is 1-based")
    exponent = min(attempt_number - 1, 32)
    ceiling = min(policy.max_delay_seconds, policy.base_delay_seconds * (2**exponent))
    delay = rng.uniform(0, ceiling)
    if retry_after_seconds is not None and retry_after_seconds > 0:
        delay = max(delay, min(retry_after_seconds, policy.max_delay_seconds))
    return round(delay, 3)


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: float
    reason: str


def decide_retry(
    *,
    disposition: Disposition,
    attempt_number: int,
    policy: RetryPolicy,
    rng: random.Random,
    retry_after_seconds: float | None = None,
) -> RetryDecision:
    if disposition is Disposition.SUCCESS:
        return RetryDecision(False, 0.0, "delivered")
    if disposition is Disposition.PERMANENT:
        return RetryDecision(False, 0.0, "permanent_failure")
    if attempt_number >= policy.max_attempts:
        return RetryDecision(False, 0.0, "attempts_exhausted")
    delay = compute_backoff(attempt_number, policy, rng, retry_after_seconds)
    return RetryDecision(True, delay, "retry_scheduled")
