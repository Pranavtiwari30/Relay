# Relay — Product Requirements

**Relay is an event-driven integration and reliability platform.** It lets a team configure an
integration between two systems, then reliably ingests events from the source, validates and
transforms them, delivers them to the destination's API, and gives operators the tools to see,
diagnose and recover from failures.

This is a portfolio project. It has no customers, no production usage and no business metrics, and
this document does not invent any. Its purpose is to demonstrate software engineering: APIs,
system integration, reliability, testing and cloud operations.

Status of each requirement is tracked in [PROJECT_STATE.md](PROJECT_STATE.md).

## 1. Problem

Independent systems disagree about payload shape, authentication and reliability. Connecting two
of them means writing glue that must:

- authenticate the sender and survive duplicate or malformed input;
- map one schema onto another without losing or corrupting data;
- deliver to an API that is sometimes slow, down, throttling or rejecting;
- retry safely, and stop retrying when it is pointless;
- leave a record good enough to answer "what happened to event X?" weeks later.

Each of these is easy to get subtly wrong, and the failure is usually silent data loss or
duplication. Relay provides one dependable runtime and one interface for all of it.

### Illustrative flow

A storefront sends an `order.created` webhook with fields like `customer`, `qty` and `unit_cost`.
Relay verifies its signature, stores it, validates it, maps it to a canonical order, and POSTs it to
a fulfilment API. If the fulfilment API returns 503, Relay records the attempt and retries with
backoff. If it keeps failing, the event is dead-lettered with a visible reason. An operator inspects
it, optionally asks for a diagnosis, and replays it once the destination is healthy.

## 2. Principles

1. **Software first.** Event processing works with every AI provider absent.
2. **Reliability over magic.** Explicit states, persisted history, bounded retries, recoverable
   failures.
3. **Evidence over confidence.** Diagnoses separate observed facts from hypotheses and cite stored
   records.
4. **Human control.** Nothing automated changes configuration or replays events.
5. **Honest guarantees.** At-least-once delivery with idempotency protections — not exactly-once.
6. **Free-tier-aware, not falsely free.** Costs and limits are documented with sources.

## 3. Users

| Persona | Needs |
|---|---|
| **Integration administrator** | Configure connections, schemas, mappings, filters and retry settings; activate, pause and disable integrations; manage members. |
| **Operations engineer** | Monitor events, investigate failures, pause/resume delivery, replay events, record decisions. |
| **Viewer / reviewer** | Inspect configuration and history without being able to change anything. |
| **Source developer** | Send a signed webhook and get a clear, actionable response. |

## 4. Inputs and outputs

**Inputs:** JSON webhooks (optionally HMAC-signed, optionally carrying a source event id);
integration configuration; simulator-generated events; operator actions in the UI.

**Outputs:** the transformed payload delivered to the destination; a persisted event with its full
state history; delivery attempts with status, duration and a redacted response summary;
dead-lettered events and audited replays; a dashboard computed from stored data; optional
investigation reports; structured logs, health endpoints and an OpenAPI document.

## 5. Functional requirements

| ID | Requirement | Summary of what "done" means |
|---|---|---|
| **FR-01** | Integration configuration | Create, view, update, activate, pause, resume, disable. Every configuration change creates an immutable version; each event records the version that processed it. Validation runs before activation and returns located, actionable errors. |
| **FR-02** | Adapters | Source and destination adapter protocols plus a registry, so a new connector does not touch core processing. Ships a generic JSON webhook source, a generic HTTP destination, a demo source simulator and a built-in demo destination. |
| **FR-03** | Ingestion | Validates content type, size, JSON and HMAC signature. Persists the event **and** its outbox row in one transaction before acknowledging. Deduplicates on source event identity via a database unique constraint. A correlation id follows the event everywhere. |
| **FR-04** | Deterministic transformation | A restricted declarative DSL: path mapping, rename, typed conversions, defaults, required fields, allowlisted filters, bounded nested access. Output validated against a schema. No `eval`, no user code. |
| **FR-05** | Asynchronous processing | Pub/Sub in the cloud; a tested PostgreSQL-backed queue locally. API and worker are separate deployable entry points. Messages are acknowledged only after the outcome is persisted. |
| **FR-06** | Retry, dead-letter, replay | Failures are classified. Only retryable ones are retried, with bounded exponential backoff and jitter. Exhausted or permanent failures are dead-lettered with a reason. Replay is authorised, audited, idempotent and never erases history. |
| **FR-07** | Operations UI | Dashboard, integrations, editor, event explorer, event detail with timeline, failures view, replay, investigations, audit log, settings. Loading/empty/error/forbidden states throughout. No fabricated numbers. |
| **FR-08** | Authentication and RBAC | Sessions with a vetted password hash. Administrator / Operator / Viewer roles enforced by the API. Workspace isolation. Explicit CORS allowlist and CSRF protection. |
| **FR-09** | Audit | Configuration, connection, membership, replay and recommendation-decision changes are recorded with actor, action, entity, time and safe metadata. |
| **FR-10** | Investigation assistant | Runs only on request. Read-only, workspace-scoped tools (also exposed over MCP). Structured report with evidence references validated server-side. Two or more provider slots with fallback, plus a deterministic model-free path. Cannot mutate anything. |
| **FR-11** | Observability | `/healthz`, `/readyz`, structured logs with correlation ids across every hop, and operational figures (throughput, outcomes, attempt latency, retry/dead-letter counts, oldest pending work, investigation outcomes). |

## 6. Non-functional requirements

- **Security:** secure defaults, least privilege, no secrets in Git or logs, SSRF defences on
  destination URLs, redaction before display and before model context.
- **Bounded everything:** payload size, timeouts, retry attempts, broker redelivery, agent steps,
  tool-result size, wall-clock time of an investigation.
- **Consistency:** transactional state changes; idempotent ingestion, attempt creation and replay.
- **Operability:** versioned, reversible migrations; consistent error envelope; documented
  pagination; correlation ids.
- **Testability:** core logic runs without GCP and without a model.
- **Economy:** no service without a real need; runs within published free allowances at demo
  volume ([cost model](deployment/free-tier-cost-model.md)).

## 7. Acceptance criteria

Each journey is covered by automated tests; the mapping is in
[quality/testing-strategy.md](quality/testing-strategy.md).

| # | Journey |
|---|---|
| AC-1 | Create, validate and activate an integration. |
| AC-2 | Send a webhook → persisted → queued → transformed → delivered → visible in the UI. |
| AC-3 | Send the same event again and observe deduplication. |
| AC-4 | Send a malformed payload: actionable validation failure, no delivery. |
| AC-5 | Simulate timeout / 429 / 500: retries occur and attempt history is recorded. |
| AC-6 | Exhaust attempts: the event is dead-lettered with a reason. |
| AC-7 | Replay as an authorised operator; history is preserved. |
| AC-8 | A Viewer cannot perform restricted actions by calling the API directly. |
| AC-9 | Simulated publish failure and worker crash both recover durably. |
| AC-10 | An investigation cites real evidence, and the assistant cannot change integration state. |
| AC-11 | The system runs locally with no cloud credentials and no model API key. |
| AC-12 | Build and deploy through a protected workflow with a smoke test, once deployment credentials exist. |

## 8. Success criteria

Measured by tests and repeatable demonstration, not by usage:

- every acceptance journey has at least one passing automated test;
- core logic (`relay_core`) has ≥ 80% line-and-branch coverage;
- the demo walkthrough reproduces success, retry, dead-letter and replay on a clean checkout;
- no document claims a performance, accuracy, savings, usage or deployment result that was not
  measured.

## 9. Non-goals

- Customer billing, a commercial launch, a marketing site, or claims about real customers.
- A no-code workflow builder or any execution of user-supplied code.
- Exactly-once side effects in external systems.
- A general-purpose scheduler, DAG engine or worker-claiming framework.
- Kubernetes, multi-cloud, GPUs, model training, vector databases, RAG, or many vendor connectors.
- Any AI-initiated change to configuration or delivery without human approval.
