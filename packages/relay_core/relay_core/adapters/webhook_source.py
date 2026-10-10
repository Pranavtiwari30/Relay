"""Generic JSON webhook source: HMAC authentication and event identity extraction."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay_core.adapters.base import InboundRequest, SourceIdentity
from relay_core.domain.signatures import SignatureError, verify_signature
from relay_core.domain.transform.paths import MISSING, PathSyntaxError, get_path, parse_path
from relay_core.errors import Issue, ServiceUnavailableError, UnauthenticatedError


class WebhookSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature_header: str = Field(default="X-Relay-Signature", pattern=r"^[A-Za-z0-9-]{1,64}$")
    timestamp_header: str = Field(default="X-Relay-Timestamp", pattern=r"^[A-Za-z0-9-]{1,64}$")
    # Signing "<timestamp>.<body>" bounds replay of a captured request. Turn off
    # only for senders that can sign the body alone.
    require_timestamp: bool = True
    event_id_header: str | None = Field(default="X-Relay-Event-Id", pattern=r"^[A-Za-z0-9-]{1,64}$")
    event_id_path: str | None = Field(default=None, max_length=512)
    event_type_header: str | None = Field(
        default="X-Relay-Event-Type", pattern=r"^[A-Za-z0-9-]{1,64}$"
    )
    event_type_path: str | None = Field(default=None, max_length=512)


def _scalar_text(value: Any, limit: int) -> str | None:
    if isinstance(value, bool) or value is None or value is MISSING:
        return None
    if isinstance(value, str | int):
        text = str(value).strip()
        if text and len(text) <= limit and text.isprintable():
            return text
    return None


class WebhookSourceAdapter:
    adapter_type = "webhook"

    def _parse(self, config: dict[str, Any]) -> WebhookSourceConfig:
        return WebhookSourceConfig.model_validate(config)

    def validate_config(self, config: dict[str, Any], *, has_secret: bool) -> list[Issue]:
        try:
            parsed = self._parse(config)
        except ValidationError as exc:
            return [
                Issue("invalid_config", ".".join(str(p) for p in error["loc"]) or "$", error["msg"])
                for error in exc.errors(include_url=False, include_input=False)
            ]
        issues: list[Issue] = []
        for name in ("event_id_path", "event_type_path"):
            value = getattr(parsed, name)
            if value is not None:
                try:
                    parse_path(value)
                except PathSyntaxError as exc:
                    issues.append(Issue("invalid_path", name, str(exc)))
        if not has_secret:
            issues.append(
                Issue(
                    "unsigned_webhook",
                    "secret_ref",
                    "no signing secret: anyone who learns the webhook URL can submit events",
                    severity="warning",
                )
            )
        if parsed.event_id_header is None and parsed.event_id_path is None:
            issues.append(
                Issue(
                    "no_event_identity",
                    "event_id_path",
                    "no event id configured: duplicates are detected by identical body only",
                    severity="warning",
                )
            )
        return issues

    def authenticate(
        self,
        config: dict[str, Any],
        secret: str | None,
        request: InboundRequest,
        *,
        tolerance_seconds: int,
    ) -> None:
        if secret is None:
            return
        parsed = self._parse(config)
        try:
            verify_signature(
                secret=secret.encode(),
                body=request.body,
                signature_header=request.headers.get(parsed.signature_header.lower()),
                timestamp_header=request.headers.get(parsed.timestamp_header.lower()),
                require_timestamp=parsed.require_timestamp,
                now=request.received_at,
                tolerance_seconds=tolerance_seconds,
            )
        except SignatureError as exc:
            # One generic message for every failure mode: the caller learns
            # nothing about which check failed. The precise code is logged.
            raise UnauthenticatedError(
                "webhook signature verification failed",
                code="invalid_signature",
                details=[{"reason": exc.code}],
            ) from exc

    def identify(
        self, config: dict[str, Any], request: InboundRequest, payload: dict[str, Any]
    ) -> SourceIdentity:
        parsed = self._parse(config)

        def lookup(header: str | None, path: str | None, limit: int) -> str | None:
            if header is not None:
                found = _scalar_text(request.headers.get(header.lower()), limit)
                if found is not None:
                    return found
            if path is not None:
                try:
                    return _scalar_text(get_path(payload, parse_path(path)), limit)
                except PathSyntaxError:
                    return None
            return None

        return SourceIdentity(
            source_event_id=lookup(parsed.event_id_header, parsed.event_id_path, 255),
            event_type=lookup(parsed.event_type_header, parsed.event_type_path, 128),
        )


def require_secret(secret: str | None, *, has_ref: bool) -> None:
    """A configured-but-unresolvable signing secret must fail closed, not open."""
    if has_ref and secret is None:
        raise ServiceUnavailableError(
            "this webhook endpoint is not ready: its signing secret is not configured",
            code="secret_unavailable",
        )
