---
paths:
  - "packages/**/*.py"
  - "services/**/*.py"
  - "scripts/**/*.py"
---

# Backend rules

## Layers

- `relay_core/domain/` is pure: no database, network, clock or randomness. Take those as
  parameters. Anything here must be unit-testable with no fixtures.
- `relay_core/services/` orchestrates and **owns transaction boundaries**. A service function takes
  a `Session` and lets the caller commit, or takes a `SessionFactory` and manages its own
  transaction. State which in the docstring when it matters.
- Routers in `services/api` and `services/worker` are thin: parse, authorise, call one service,
  commit, serialise. No business rules in a router.
- `relay_core` must not import FastAPI. Shared HTTP behaviour goes in `relay_core/asgi.py` as plain
  ASGI.

## State and transactions

- Change `Event.status` only via `event_state.transition()`. Lock the event row first
  (`SELECT … FOR UPDATE`).
- Lock order is always **event row, then outbox row**.
- Never hold a lock or an open transaction across a network call.
- Anything that must reach the queue goes through `outbox.schedule_delivery()` in the same
  transaction as the state change.

## Types and style

- Python 3.11+, full type hints, `mypy --strict` clean. `ruff format` and `ruff check` clean.
- Pydantic models for anything crossing a boundary (requests, config, adapter config). Request
  models forbid unknown fields.
- Pass time and randomness in (`clock`, `rng`); do not call `datetime.now()` or `random` directly
  in services.

## Errors and logging

- Raise `RelayError` subclasses with a stable `code`. Messages are for API consumers: no SQL, no
  stack traces, no secret values, no echo of rejected input.
- Do not swallow exceptions. A deliberate best-effort path uses `contextlib.suppress` or logs with
  `exc_info`, and says why in a comment.
- Log identifiers and classifications, never payloads or secrets. Use `bind_log_context` so
  `correlation_id` and `event_id` are on every line. Do not bind log context inside a FastAPI
  generator dependency (it runs in a different thread context).

## Database

- Schema changes need an Alembic migration with a working `downgrade`. Do not edit an applied
  migration. `alembic check` must report no drift.
- Migrations must be backward compatible with the previous code revision (add first; remove in a
  later release).
- Every tenant-owned query filters on `workspace_id`. A missing or foreign id returns 404.
- Use `INSERT … ON CONFLICT` and unique constraints for idempotency, not check-then-insert.

## Adapters

- New connectors implement `SourceAdapter` / `DestinationAdapter` and are registered in
  `adapters/registry.py`. Do not add `if adapter_type == …` branches in core code.
- `deliver()` returns a classified `DeliveryResult`; it does not raise for timeouts, transport
  errors or HTTP errors, and it enforces the timeout as a hard deadline.
