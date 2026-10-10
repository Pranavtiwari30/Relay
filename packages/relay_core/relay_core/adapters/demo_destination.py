"""Built-in demo destination adapter.

Delivers over real HTTP to the demo "fulfilment API" that the Relay API
service hosts (``/demo/fulfillment/{workspace_id}/orders``), so the demo
exercises the same network path, timeouts and status handling as any other
destination. The demo base URL is operator configuration, not user input, so
it is the one target allowed to be a private address.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from relay_core.adapters.base import DeliveryRequest, DeliveryResult
from relay_core.adapters.http_destination import (
    HttpAuth,
    HttpDestinationAdapter,
    HttpDestinationConfig,
)
from relay_core.domain.retry import Disposition, FailureCategory
from relay_core.domain.urlsafety import Resolver, UrlPolicy, system_resolver
from relay_core.errors import Issue


class DemoDestinationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoDestinationAdapter(HttpDestinationAdapter):
    adapter_type = "demo"

    def __init__(
        self,
        base_url: str,
        *,
        max_response_bytes: int = 65_536,
        resolver: Resolver = system_resolver,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        parts = urlsplit(self._base_url)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        policy = UrlPolicy(
            allow_http=parts.scheme == "http",
            allow_private_hosts=frozenset({f"{host}:{port}"}),
        )
        super().__init__(
            policy, max_response_bytes=max_response_bytes, resolver=resolver, transport=transport
        )

    def validate_config(self, config: dict[str, Any], *, has_secret: bool) -> list[Issue]:
        try:
            DemoDestinationConfig.model_validate(config)
        except ValidationError:
            return [Issue("invalid_config", "$", "the demo destination takes no configuration")]
        if not has_secret:
            return [
                Issue(
                    "secret_required",
                    "secret_ref",
                    "the demo destination needs its API token reference",
                )
            ]
        return []

    def deliver(
        self, config: dict[str, Any], secret: str | None, request: DeliveryRequest
    ) -> DeliveryResult:
        started = time.monotonic()
        try:
            http_config = HttpDestinationConfig(
                url=f"{self._base_url}/demo/fulfillment/{request.workspace_id}/orders",
                method="POST",
                auth=HttpAuth(type="bearer"),
            )
        except ValidationError:
            return self._failed(
                started, Disposition.PERMANENT, FailureCategory.CONFIGURATION,
                "demo destination base URL is not valid",
            )  # fmt: skip
        return self._send(http_config, secret, request, self._policy, started)
