# ADR-0001: A modular monolith with two deployable entry points

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

Relay has two workloads with different shapes: a request/response API (configuration, queries,
webhook ingestion) and a delivery worker driven by queue messages. They share one domain — events,
state transitions, the transformation pipeline — and one database.

## Decision

One repository and one Python workspace with three packages:

- `relay_core` — domain, persistence, adapters, application services, investigation;
- `relay_api` — the FastAPI control plane and webhook edge;
- `relay_worker` — the FastAPI push receiver, outbox dispatcher and lease reaper.

The API and worker are separate processes and separate Cloud Run services. Inside `relay_core`,
dependencies point inward (services → domain); the domain layer is pure.

## Alternatives considered

- **A single process.** Simplest, but delivery load and API load could not scale or fail
  independently, and the worker could not be made unreachable from the internet.
- **Microservices** (ingestion, transformation, delivery, audit…). Each boundary would add a
  network hop, a failure mode and a versioned contract around logic that changes together. With one
  team and one database it buys only overhead.
- **A workflow engine.** Out of scope by design; Relay is an integration product, not a scheduler.

## Consequences

- One schema, one migration history, one set of domain rules: the state machine cannot diverge
  between services.
- Both services ship the same code and differ only in entry point, so a deploy can promote one
  commit to both.
- The boundary between packages is a convention enforced by review and import direction, not by a
  network. `relay_core` must stay free of FastAPI imports to keep it testable in isolation.
