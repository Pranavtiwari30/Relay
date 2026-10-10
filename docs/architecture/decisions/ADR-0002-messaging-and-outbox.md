# ADR-0002: Transactional outbox, push delivery, and at-least-once semantics

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

Accepting an event and enqueueing its delivery touch two systems (PostgreSQL and a broker) that
cannot commit atomically. Delivery calls a third-party API whose response can be lost. The platform
must scale to zero, so nothing can depend on a long-running poller.

## Decision

1. **Transactional outbox.** The event change and an `outbox_messages` row commit together. A
   dispatcher publishes rows using lease-based claims with `FOR UPDATE SKIP LOCKED`, and marks a row
   published only after the broker confirms.
2. **Push, not pull.** The worker is an HTTP service receiving Pub/Sub push envelopes. No process
   needs to stay alive to poll.
3. **Messages carry identifiers only** (`event_id`, `cycle`, `attempt`). The database is the source
   of truth; a message is a wake-up, not a copy of the data.
4. **At-least-once.** Duplicate messages are expected and discarded by comparing `(cycle, attempt)`
   with the event's state. Every outbound request carries a stable idempotency key.
5. **Two retry mechanisms with separate jobs.** Broker redelivery handles worker failure;
   application retries handle destination outcomes and are scheduled through the same outbox. The
   worker acknowledges every outcome it has persisted, so the two never multiply.
6. **Locally, a PostgreSQL-backed queue** reproduces the same push envelope, redelivery, attempt
   counter and dead-letter hand-off, so the worker has one code path.

## Alternatives considered

- **Publish directly from the request handler.** Loses events when the process dies between commit
  and publish.
- **Change data capture.** Solves the same problem with far more infrastructure.
- **The Pub/Sub emulator for local use.** Needs Java or Docker; the local queue runs with nothing
  but PostgreSQL and is itself covered by tests.
- **Let the broker schedule retries** (nack with backoff). Loses control of the schedule
  (`Retry-After`, per-integration policy, jitter) and conflates the two failure classes.
- **Exactly-once.** Not achievable against an arbitrary HTTP API whose response can be lost;
  claiming it would be false.

## Consequences

- No acknowledged event is lost to a crash or a broker outage; duplicates are possible and handled.
- Something must run the dispatcher. Locally a loop does; in the cloud one Cloud Scheduler job does.
  Its period bounds retry latency (see ADR-0004).
- The outbox table needs trimming (done by the dispatcher tick).

## Follow-up

Event-driven wake-ups (a Cloud Tasks task scheduled for `next_attempt_at`) would remove the latency
floor without polling. It is not implemented; the scheduled dispatcher would remain as the safety
net.
