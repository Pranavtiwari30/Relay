"""Publisher implementations and the factory that selects one from configuration."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from relay_core.config import Settings
from relay_core.db.session import SessionFactory
from relay_core.messaging.base import Publisher, PublishError
from relay_core.messaging.local_queue import LocalQueuePublisher

logger = logging.getLogger(__name__)


class MemoryPublisher:
    """In-process publisher for tests. Records messages and can be told to fail."""

    def __init__(self) -> None:
        self.messages: list[tuple[dict[str, Any], dict[str, str]]] = []
        self.fail_with: Exception | None = None
        self.fail_next: int = 0

    def publish(self, data: dict[str, Any], attributes: dict[str, str]) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        if self.fail_next > 0:
            self.fail_next -= 1
            raise PublishError("simulated broker outage")
        self.messages.append((data, attributes))
        return f"mem-{uuid.uuid4().hex[:12]}"


class PubSubPublisher:
    """Google Cloud Pub/Sub publisher.

    The client library is imported lazily so local and test runs need neither
    the dependency's gRPC stack at import time nor any credentials.
    """

    def __init__(
        self, project_id: str, topic: str, *, timeout_seconds: float, client: Any = None
    ) -> None:
        if client is None:
            from google.cloud import pubsub_v1

            client = pubsub_v1.PublisherClient()
        self._client = client
        self._topic_path = client.topic_path(project_id, topic)
        self._timeout = timeout_seconds

    def publish(self, data: dict[str, Any], attributes: dict[str, str]) -> str:
        body = json.dumps(data, separators=(",", ":")).encode()
        try:
            future = self._client.publish(self._topic_path, body, **attributes)
            # Block until the service confirms: only then may the outbox row be
            # marked published.
            return str(future.result(timeout=self._timeout))
        except Exception as exc:
            raise PublishError(f"Pub/Sub publish failed ({type(exc).__name__})") from exc


def build_publisher(settings: Settings, session_factory: SessionFactory) -> Publisher:
    if settings.queue_backend == "pubsub":
        assert settings.pubsub_project_id is not None  # enforced by Settings validation
        return PubSubPublisher(
            settings.pubsub_project_id,
            settings.pubsub_topic,
            timeout_seconds=settings.pubsub_publish_timeout_seconds,
        )
    if settings.queue_backend == "local":
        return LocalQueuePublisher(session_factory, settings.pubsub_topic)
    return MemoryPublisher()
