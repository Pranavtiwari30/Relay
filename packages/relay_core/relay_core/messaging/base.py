"""Broker-neutral message contract.

The worker always receives a Google Pub/Sub *push envelope*. In the cloud it
comes from a Pub/Sub push subscription; locally the development queue produces
the identical shape. The worker therefore has one code path.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

MESSAGE_VERSION = 1


class PublishError(Exception):
    """The broker did not confirm the message. The outbox row stays pending."""


class MalformedMessageError(Exception):
    """The message can never be processed. It is acknowledged, not redelivered."""


@dataclass(frozen=True)
class DeliveryMessage:
    """Instruction to run one delivery attempt. Carries identifiers only, never payloads."""

    event_id: UUID
    workspace_id: UUID
    cycle: int
    attempt: int
    correlation_id: str

    def to_data(self) -> dict[str, Any]:
        return {
            "v": MESSAGE_VERSION,
            "event_id": str(self.event_id),
            "workspace_id": str(self.workspace_id),
            "cycle": self.cycle,
            "attempt": self.attempt,
            "correlation_id": self.correlation_id,
        }

    def attributes(self) -> dict[str, str]:
        return {"event_id": str(self.event_id), "correlation_id": self.correlation_id}

    @classmethod
    def from_data(cls, data: Any) -> DeliveryMessage:
        try:
            if not isinstance(data, dict) or data.get("v") != MESSAGE_VERSION:
                raise ValueError("unsupported message version")
            cycle, attempt = data["cycle"], data["attempt"]
            if isinstance(cycle, bool) or isinstance(attempt, bool):
                raise ValueError("counters must be integers")
            if (
                not isinstance(cycle, int)
                or not isinstance(attempt, int)
                or cycle < 1
                or attempt < 1
            ):
                raise ValueError("counters must be positive integers")
            correlation = data["correlation_id"]
            if not isinstance(correlation, str) or not 1 <= len(correlation) <= 64:
                raise ValueError("invalid correlation id")
            return cls(
                event_id=UUID(str(data["event_id"])),
                workspace_id=UUID(str(data["workspace_id"])),
                cycle=cycle,
                attempt=attempt,
                correlation_id=correlation,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedMessageError(f"malformed delivery message: {exc}") from exc


class Publisher(Protocol):
    def publish(self, data: dict[str, Any], attributes: dict[str, str]) -> str:
        """Hand a message to the broker and return its id, or raise ``PublishError``.

        Must only return once the broker has durably accepted the message.
        """
        ...


@dataclass(frozen=True)
class PushMessage:
    data: Any
    attributes: dict[str, str]
    message_id: str
    delivery_attempt: int | None


def encode_push_envelope(
    data: dict[str, Any],
    attributes: dict[str, str],
    *,
    message_id: str,
    subscription: str,
    delivery_attempt: int | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "message": {
            "data": base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode(),
            "attributes": attributes,
            "messageId": message_id,
        },
        "subscription": subscription,
    }
    if delivery_attempt is not None:
        envelope["deliveryAttempt"] = delivery_attempt
    return envelope


def decode_push_envelope(body: Any) -> PushMessage:
    try:
        message = body["message"]
        raw = base64.b64decode(message["data"], validate=True)
        data = json.loads(raw)
        attributes = message.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be an object")
        attempt = body.get("deliveryAttempt")
        return PushMessage(
            data=data,
            attributes={str(k): str(v) for k, v in attributes.items()},
            message_id=str(message.get("messageId") or message.get("message_id") or ""),
            delivery_attempt=attempt
            if isinstance(attempt, int) and not isinstance(attempt, bool)
            else None,
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise MalformedMessageError(f"malformed push envelope: {type(exc).__name__}") from exc
