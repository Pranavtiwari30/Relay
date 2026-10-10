"""Generic HTTP destination.

Every attempt re-validates the URL, resolves DNS, connects to the validated
address directly, never follows redirects, enforces a hard deadline, reads a
bounded amount of the response, and returns a classified ``DeliveryResult``.
"""

from __future__ import annotations

import base64
import json
import time
from email.utils import parsedate_to_datetime
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from relay_core.adapters.base import DeliveryRequest, DeliveryResult
from relay_core.domain.redaction import is_sensitive_key, summarise_text
from relay_core.domain.retry import Disposition, FailureCategory, classify_http_status
from relay_core.domain.urlsafety import (
    DestinationBlockedError,
    DestinationResolutionError,
    Resolver,
    UrlPolicy,
    check_url_static,
    resolve_destination,
    system_resolver,
)
from relay_core.errors import Issue

USER_AGENT: Final = "Relay/0.1 (+event-delivery)"
_HEADER_NAME: Final = r"^[A-Za-z0-9-]{1,64}$"
# Headers an operator may not set: hop-by-hop, framing, credentials (use
# ``auth`` with a secret reference instead), and Relay's own namespace.
_FORBIDDEN_HEADERS: Final = frozenset(
    {
        "host", "content-length", "content-type", "transfer-encoding", "connection",
        "keep-alive", "upgrade", "te", "trailer", "expect", "authorization",
        "proxy-authorization", "cookie", "set-cookie", "forwarded", "user-agent",
    }
)  # fmt: skip
_FORBIDDEN_PREFIXES: Final = ("x-relay-", "x-forwarded-", "proxy-", "sec-")


class HttpAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "bearer", "header", "basic"] = "none"
    header_name: str | None = Field(default=None, pattern=_HEADER_NAME)
    username: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> HttpAuth:
        if self.type == "header" and not self.header_name:
            raise ValueError("auth type 'header' requires header_name")
        if self.type == "basic" and not self.username:
            raise ValueError("auth type 'basic' requires username")
        name = (self.header_name or "").lower()
        if self.type == "header" and (
            name in _FORBIDDEN_HEADERS - {"authorization"} or name.startswith(_FORBIDDEN_PREFIXES)
        ):
            raise ValueError(f"'{self.header_name}' cannot be used as an auth header")
        return self


class HttpDestinationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    method: Literal["POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    auth: HttpAuth = Field(default_factory=HttpAuth)
    idempotency_header: str | None = Field(default="Idempotency-Key", pattern=_HEADER_NAME)


def _check_static_headers(headers: dict[str, str]) -> list[Issue]:
    issues: list[Issue] = []
    for name, value in headers.items():
        lowered = name.lower()
        where = f"headers.{name}"
        if (
            not name.isascii()
            or not all(ch.isalnum() or ch == "-" for ch in name)
            or len(name) > 64
        ):
            issues.append(
                Issue("invalid_header", where, "header names may contain letters, digits and '-'")
            )
        elif lowered in _FORBIDDEN_HEADERS or lowered.startswith(_FORBIDDEN_PREFIXES):
            issues.append(
                Issue(
                    "forbidden_header", where, "this header is managed by Relay and cannot be set"
                )
            )
        elif is_sensitive_key(lowered):
            issues.append(
                Issue(
                    "secret_in_header",
                    where,
                    "credentials must not be stored in configuration; use 'auth' with a secret reference",
                )
            )
        if (
            len(value) > 1024
            or not value.isascii()
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
        ):
            issues.append(
                Issue(
                    "invalid_header",
                    where,
                    "header values must be printable ASCII, at most 1024 chars",
                )
            )
    return issues


def _pydantic_issues(exc: ValidationError) -> list[Issue]:
    issues = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]) or "$"
        issues.append(Issue("invalid_config", location, error["msg"].removeprefix("Value error, ")))
    return issues


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit() and len(value) <= 9:
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


class HttpDestinationAdapter:
    adapter_type = "http"

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        max_response_bytes: int = 65_536,
        resolver: Resolver = system_resolver,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._max_response_bytes = max_response_bytes
        self._resolver = resolver
        # trust_env=False: ignore proxy environment variables, which would
        # otherwise route requests around the address pinning below.
        self._client = httpx.Client(
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )

    def close(self) -> None:
        self._client.close()

    # -- configuration ------------------------------------------------------

    def validate_config(self, config: dict[str, Any], *, has_secret: bool) -> list[Issue]:
        try:
            parsed = HttpDestinationConfig.model_validate(config)
        except ValidationError as exc:
            return _pydantic_issues(exc)
        issues = _check_static_headers(parsed.headers)
        reason = check_url_static(parsed.url, self._policy)
        if reason is not None:
            issues.append(Issue("destination_not_allowed", "url", reason))
        if "?" in parsed.url and any(
            is_sensitive_key(part.split("=", 1)[0])
            for part in parsed.url.split("?", 1)[1].split("&")
        ):
            issues.append(
                Issue(
                    "secret_in_url", "url", "credentials must not be placed in the URL query string"
                )
            )
        if parsed.auth.type != "none" and not has_secret:
            issues.append(
                Issue(
                    "secret_required",
                    "secret_ref",
                    f"auth type '{parsed.auth.type}' needs a secret reference",
                )
            )
        return issues

    # -- delivery -----------------------------------------------------------

    def deliver(
        self, config: dict[str, Any], secret: str | None, request: DeliveryRequest
    ) -> DeliveryResult:
        started = time.monotonic()
        try:
            parsed = HttpDestinationConfig.model_validate(config)
        except ValidationError:
            return self._failed(
                started, Disposition.PERMANENT, FailureCategory.CONFIGURATION,
                "destination configuration is no longer valid",
            )  # fmt: skip
        return self._send(parsed, secret, request, self._policy, started)

    def _send(
        self,
        config: HttpDestinationConfig,
        secret: str | None,
        request: DeliveryRequest,
        policy: UrlPolicy,
        started: float,
    ) -> DeliveryResult:
        if config.auth.type != "none" and not secret:
            return self._failed(
                started, Disposition.PERMANENT, FailureCategory.CONFIGURATION,
                "destination credential could not be resolved from its secret reference",
                method=config.method,
            )  # fmt: skip
        try:
            resolved = resolve_destination(config.url, policy, self._resolver)
        except DestinationBlockedError as exc:
            return self._failed(
                started, Disposition.PERMANENT, FailureCategory.BLOCKED_DESTINATION,
                f"destination blocked: {exc.reason}", method=config.method,
            )  # fmt: skip
        except DestinationResolutionError as exc:
            return self._failed(
                started,
                Disposition.RETRYABLE,
                FailureCategory.TRANSPORT,
                str(exc),
                method=config.method,
            )

        parsed = resolved.parsed
        headers = {
            **config.headers,
            "Host": parsed.host_header,
            "Content-Type": "application/json",
            "Accept": "application/json, */*;q=0.5",
            "User-Agent": USER_AGENT,
            "X-Relay-Event-Id": str(request.event_id),
            "X-Relay-Correlation-Id": request.correlation_id,
            "X-Relay-Attempt": str(request.attempt_number),
        }
        if config.idempotency_header:
            headers[config.idempotency_header] = request.idempotency_key
        if secret and config.auth.type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif secret and config.auth.type == "header" and config.auth.header_name:
            headers[config.auth.header_name] = secret
        elif secret and config.auth.type == "basic":
            token = base64.b64encode(f"{config.auth.username}:{secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        timeout = request.timeout_seconds
        deadline = started + timeout
        extensions: dict[str, Any] = {}
        if parsed.scheme == "https" and parsed.literal_ip is None:
            # We connect to the pinned IP; TLS must still verify the real hostname.
            extensions["sni_hostname"] = parsed.host
        body = json.dumps(request.payload, separators=(",", ":"), ensure_ascii=False).encode()
        safe = {"method": config.method, "url": parsed.display}

        try:
            with self._client.stream(
                config.method,
                resolved.connect_url,
                headers=headers,
                content=body,
                timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
                extensions=extensions,
            ) as response:
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise httpx.ReadTimeout("deadline exceeded while reading response")
                    remaining = self._max_response_bytes - received
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    received += len(chunk[:remaining])
                status = response.status_code
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException:
            return self._failed(
                started, Disposition.RETRYABLE, FailureCategory.TIMEOUT,
                f"no complete response within {timeout:g}s; the destination may still have "
                "processed the request",
                **safe,
            )  # fmt: skip
        except (httpx.RemoteProtocolError, httpx.DecodingError):
            return self._failed(
                started, Disposition.RETRYABLE, FailureCategory.INVALID_RESPONSE,
                "destination sent a malformed HTTP response", **safe,
            )  # fmt: skip
        except httpx.TransportError as exc:
            return self._failed(
                started, Disposition.RETRYABLE, FailureCategory.TRANSPORT,
                f"could not reach destination ({type(exc).__name__})", **safe,
            )  # fmt: skip

        try:
            text = b"".join(chunks).decode(encoding, errors="replace")
        except LookupError:
            text = b"".join(chunks).decode("utf-8", errors="replace")
        disposition, category = classify_http_status(status)
        error = None
        if disposition is not Disposition.SUCCESS:
            error = f"destination responded with HTTP {status}"
            if category is FailureCategory.REDIRECT:
                error += " (redirects are not followed)"
        return DeliveryResult(
            disposition=disposition,
            duration_ms=int((time.monotonic() - started) * 1000),
            failure_category=category,
            http_status=status,
            response_summary=summarise_text(text) if text.strip() else None,
            retry_after_seconds=retry_after if disposition is Disposition.RETRYABLE else None,
            error_message=error,
            request_method=config.method,
            request_url=parsed.display,
        )

    @staticmethod
    def _failed(
        started: float,
        disposition: Disposition,
        category: FailureCategory,
        message: str,
        *,
        method: str | None = None,
        url: str | None = None,
    ) -> DeliveryResult:
        return DeliveryResult(
            disposition=disposition,
            duration_ms=int((time.monotonic() - started) * 1000),
            failure_category=category,
            error_message=message,
            request_method=method,
            request_url=url,
        )
