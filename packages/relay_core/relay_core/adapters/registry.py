"""Adapter registry: a map from connector type to implementation."""

from __future__ import annotations

from relay_core.adapters.base import DestinationAdapter, SourceAdapter
from relay_core.adapters.demo_destination import DemoDestinationAdapter
from relay_core.adapters.http_destination import HttpDestinationAdapter
from relay_core.adapters.webhook_source import WebhookSourceAdapter
from relay_core.config import Settings
from relay_core.domain.urlsafety import UrlPolicy
from relay_core.errors import ValidationFailedError


class AdapterRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, SourceAdapter] = {}
        self._destinations: dict[str, DestinationAdapter] = {}

    def register_source(self, adapter: SourceAdapter) -> None:
        if adapter.adapter_type in self._sources:
            raise ValueError(f"source adapter '{adapter.adapter_type}' is already registered")
        self._sources[adapter.adapter_type] = adapter

    def register_destination(self, adapter: DestinationAdapter) -> None:
        if adapter.adapter_type in self._destinations:
            raise ValueError(f"destination adapter '{adapter.adapter_type}' is already registered")
        self._destinations[adapter.adapter_type] = adapter

    def source(self, adapter_type: str) -> SourceAdapter:
        try:
            return self._sources[adapter_type]
        except KeyError:
            raise ValidationFailedError(
                f"unknown source adapter '{adapter_type}'",
                code="unknown_adapter",
                details=[{"supported": sorted(self._sources)}],
            ) from None

    def destination(self, adapter_type: str) -> DestinationAdapter:
        try:
            return self._destinations[adapter_type]
        except KeyError:
            raise ValidationFailedError(
                f"unknown destination adapter '{adapter_type}'",
                code="unknown_adapter",
                details=[{"supported": sorted(self._destinations)}],
            ) from None

    @property
    def source_types(self) -> list[str]:
        return sorted(self._sources)

    @property
    def destination_types(self) -> list[str]:
        return sorted(self._destinations)


def destination_policy(settings: Settings) -> UrlPolicy:
    return UrlPolicy(
        allow_http=settings.destination_allow_http,
        allow_private_hosts=frozenset(h.lower() for h in settings.destination_allow_private_hosts),
    )


def build_default_registry(settings: Settings) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register_source(WebhookSourceAdapter())
    registry.register_destination(
        HttpDestinationAdapter(
            destination_policy(settings), max_response_bytes=settings.destination_max_response_bytes
        )
    )
    registry.register_destination(
        DemoDestinationAdapter(
            settings.demo_destination_base_url,
            max_response_bytes=settings.destination_max_response_bytes,
        )
    )
    return registry
