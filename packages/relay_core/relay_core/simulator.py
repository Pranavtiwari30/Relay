"""Demo source simulator: known vendor payloads and correctly signed requests.

Used by the UI's simulator panel (in-process, through the real ingestion
service) and by ``scripts/simulate.py`` (over real HTTP). Both build requests
with the same functions, so what the demo sends is exactly what an external
system would send.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from relay_core.domain.signatures import compute_signature, format_signature

SCENARIOS: Final[dict[str, str]] = {
    "valid_order": "A well-formed order.created event that maps cleanly to the canonical order.",
    "duplicate": "Re-sends the previous valid event with the same event id (deduplicated).",
    "malformed": "Missing order number and a non-numeric quantity: fails input validation (HTTP 422).",
    "transform_error": "Passes the input schema but a quantity cannot be converted: fails in the worker.",
    "filtered": "A draft order: accepted, then skipped by the integration's status filter.",
    "invalid_signature": "Signed with the wrong secret: rejected with HTTP 401, nothing stored.",
}

_CUSTOMERS = (
    ("Ada Lovelace", "ada@example.com"),
    ("Grace Hopper", "grace@example.com"),
    ("Alan Turing", "alan@example.com"),
    ("Katherine Johnson", "katherine@example.com"),
)
_CATALOG = (("kb-100", "49.90"), ("ms-220", "24.00"), ("hd-9", "129.00"), ("cbl-3m", "7.50"))


@dataclass(frozen=True)
class SimulatedRequest:
    scenario: str
    event_id: str
    body: bytes
    headers: dict[str, str]


def build_order(event_id: str, *, sequence: int, placed_at: datetime) -> dict[str, Any]:
    """A vendor-shaped ``order.created`` payload. Deterministic for a given sequence."""
    name, email = _CUSTOMERS[sequence % len(_CUSTOMERS)]
    first = _CATALOG[sequence % len(_CATALOG)]
    second = _CATALOG[(sequence + 1) % len(_CATALOG)]
    return {
        "event_id": event_id,
        "type": "order.created",
        "order_no": f"A-{1000 + sequence}",
        "status": "paid",
        "customer": {"name": f"  {name} ", "email": email.upper()},
        "items": [
            {"sku": first[0], "qty": str(1 + sequence % 3), "unit_cost": first[1]},
            {"sku": second[0], "qty": 1, "unit_cost": second[1]},
        ],
        "currency": "usd",
        "placed_at": placed_at.isoformat(),
        # Present so the UI's redaction of sensitive fields is visible in the demo.
        "payment": {"method": "card", "card_number": "4111 1111 1111 1111"},
    }


def build_request(
    scenario: str,
    *,
    secret: str | None,
    now: datetime,
    sequence: int,
    previous_event_id: str | None = None,
) -> SimulatedRequest:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario '{scenario}'")
    event_id = f"evt_{uuid.uuid4().hex[:20]}"
    if scenario == "duplicate" and previous_event_id:
        event_id = previous_event_id
    order = build_order(event_id, sequence=sequence, placed_at=now)
    if scenario == "malformed":
        del order["order_no"]
        order["items"][0]["qty"] = {"amount": "two"}
    elif scenario == "transform_error":
        order["items"][0]["qty"] = "2 boxes"
    elif scenario == "filtered":
        order["status"] = "draft"

    body = json.dumps(order, separators=(",", ":")).encode()
    timestamp = str(int(now.timestamp()))
    headers = {
        "content-type": "application/json",
        "x-relay-event-id": event_id,
        "x-relay-event-type": order["type"],
        "x-relay-timestamp": timestamp,
    }
    signing_secret = secret
    if scenario == "invalid_signature":
        signing_secret = "not-the-real-secret"  # noqa: S105 - deliberately wrong
    if signing_secret is not None:
        headers["x-relay-signature"] = format_signature(
            compute_signature(signing_secret.encode(), body, timestamp)
        )
    return SimulatedRequest(scenario=scenario, event_id=event_id, body=body, headers=headers)


# The demo integration configuration that pairs with ``build_order``.
DEMO_INTEGRATION_CONFIG: Final[dict[str, Any]] = {
    "event_types": ["order.created"],
    "input_schema": {
        "type": "object",
        "required": ["order_no", "customer", "items", "status"],
        "properties": {
            "order_no": {"type": "string", "minLength": 1},
            "status": {"type": "string"},
            "customer": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
            },
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "required": ["sku", "qty", "unit_cost"],
                    "properties": {
                        "sku": {"type": "string"},
                        "qty": {"type": ["string", "integer"]},
                        "unit_cost": {"type": ["string", "number"]},
                    },
                },
            },
            "currency": {"type": "string"},
            "placed_at": {"type": "string"},
        },
    },
    "filters": [{"path": "status", "op": "in", "value": ["paid", "authorized"]}],
    "mappings": [
        {"target": "order_id", "source": "order_no", "required": True},
        {"target": "customer.name", "source": "customer.name", "required": True, "convert": "trim"},
        {"target": "customer.email", "source": "customer.email", "convert": "lowercase"},
        {
            "target": "lines",
            "source": "items",
            "required": True,
            "each": [
                {"target": "sku", "source": "sku", "required": True, "convert": "uppercase"},
                {"target": "quantity", "source": "qty", "required": True, "convert": "integer"},
                {
                    "target": "unit_price",
                    "source": "unit_cost",
                    "required": True,
                    "convert": "number",
                },
            ],
        },
        {"target": "currency", "source": "currency", "convert": "uppercase"},
        {"target": "ordered_at", "source": "placed_at", "convert": "datetime"},
        {"target": "source", "const": "acme-storefront"},
    ],
    "output_schema": {
        "type": "object",
        "required": ["order_id", "customer", "lines"],
        "additionalProperties": False,
        "properties": {
            "order_id": {"type": "string"},
            "customer": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "email": {"type": "string"},
                },
            },
            "lines": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["sku", "quantity", "unit_price"],
                    "properties": {
                        "sku": {"type": "string"},
                        "quantity": {"type": "integer", "minimum": 1},
                        "unit_price": {"type": "number", "minimum": 0},
                    },
                },
            },
            "currency": {"type": "string", "minLength": 3, "maxLength": 3},
            "ordered_at": {"type": "string"},
            "source": {"type": "string"},
        },
    },
    "redact_fields": [],
    "retry": {"max_attempts": 4, "base_delay_seconds": 1, "max_delay_seconds": 20},
    "delivery": {"timeout_seconds": 2},
}
