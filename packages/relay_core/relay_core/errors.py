"""Application errors with stable machine-readable codes.

Services raise these; the HTTP layer renders them as the standard error
envelope. Messages are written for API consumers and must never contain
database internals, secret values or raw destination response bodies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    """One actionable validation finding."""

    code: str
    path: str
    message: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class RelayError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or []
        self.headers = headers or {}


class BadRequestError(RelayError):
    code = "bad_request"
    http_status = 400


class UnauthenticatedError(RelayError):
    code = "unauthenticated"
    http_status = 401


class PermissionDeniedError(RelayError):
    code = "permission_denied"
    http_status = 403


class NotFoundError(RelayError):
    code = "not_found"
    http_status = 404


class ConflictError(RelayError):
    code = "conflict"
    http_status = 409


class PayloadTooLargeError(RelayError):
    code = "payload_too_large"
    http_status = 413


class UnsupportedMediaTypeError(RelayError):
    code = "unsupported_media_type"
    http_status = 415


class ValidationFailedError(RelayError):
    code = "validation_failed"
    http_status = 422

    @classmethod
    def from_issues(cls, message: str, issues: list[Issue]) -> ValidationFailedError:
        return cls(message, details=[issue.as_dict() for issue in issues])


class RateLimitedError(RelayError):
    code = "rate_limited"
    http_status = 429


class ServiceUnavailableError(RelayError):
    code = "service_unavailable"
    http_status = 503


class InvalidTransitionError(ConflictError):
    code = "invalid_state_transition"
