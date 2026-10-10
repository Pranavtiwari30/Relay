# Relay — Claude Code Master Prompt

> **Use:** Open Claude Code at the root of the repository you plan to use, then paste this prompt. Let it inspect the repo first. Never paste cloud secrets into chat or commit secrets.

## 0. Role and expected outcome

Act as the senior software engineer, solutions architect, product-minded engineer, security reviewer, and technical writer responsible for turning this repository into **Relay**, a coherent and demonstrable software project.

Inspect the repository, create the PRD/architecture/design/rules/memory documents, then implement the application in coherent increments, run tests, fix failures, and leave the repository in a working state. **Do not stop after giving me a plan or creating docs.** Continue until the implementation is complete or a real blocker exists (for example, missing credentials or an action requiring explicit authorization). Never claim an unrun test, benchmark, or deployment succeeded.

This is the **full project scope**, not a tutorial or a deliberately reduced one-day build. I may choose to work quickly; do not remove meaningful requirements just to make the project smaller. Use pragmatic trade-offs and implement tested vertical slices.

The primary target roles are **Software Development Engineer (SDE)** and **Forward Deployed Engineer (FDE)**. The main story must be software engineering, APIs, system integration, reliability, testing, cloud operations, and practical problem solving. Agentic AI is a bounded secondary capability, not the product's identity.

## 1. Project identity and positioning

**Name:** Relay — Event-Driven Integration & Reliability Platform.

**One-line description:** A platform for configuring integrations between systems, reliably ingesting and transforming events, delivering them to destination APIs, tracking failures, and helping operators diagnose and recover from failed deliveries.

**Problem:** Organizations connect independent systems with different payload schemas, authentication methods, and reliability characteristics. Engineers need to validate and transform data, deliver it to another system, investigate failures, retry safely, and understand the complete history of an event. Relay provides one interface and a dependable runtime for these tasks.

**Illustrative flow:** A vendor sends an `order.created` webhook with fields such as `customer`, `qty`, and `unit_cost`. Relay authenticates and persists it, validates the payload, maps it into a canonical order schema, applies deterministic transformations, and delivers it to a fulfillment HTTP API. On failure, Relay records the response and attempt history, retries eligible failures, and eventually marks exhausted events as dead-lettered. An operator can inspect and safely replay it. The AI assistant may investigate the failure, but must not own normal event processing.

### Product principles
1. **Software first:** core integration processing works when every model provider is unavailable.
2. **Reliability over magic:** persist event history, define explicit states, handle duplicates, and make recovery behavior understandable.
3. **Evidence over AI confidence:** separate observed facts from hypotheses and cite stored event/attempt/configuration evidence.
4. **Human control:** AI does not silently change configurations, send arbitrary requests, or replay events.
5. **Cloud is part of the system:** use real managed messaging, container deployment, secrets, CI/CD, and operations telemetry.
6. **Free-tier-aware, not falsely “free”:** target recurring $0/month under current published free allowances, document limits and risks, and never claim a budget alert is a hard cap.
7. **Do not rebuild Conductor:** this is not another generic DAG scheduler or distributed worker-claiming engine. Use managed messaging and focus on integrations and operations.
8. **Not an AI/ML project:** no model training, vector databases, RAG, or additional agents unless a demonstrated requirement justifies them.

## 2. First action: inspect the repo safely

Before editing, inspect the tree, branch, `git status`, README, dependency files, tests, and existing instructions such as `CLAUDE.md`/`AGENTS.md`. Preserve existing user work and conventions where reasonable. Never overwrite/delete uncommitted or unrelated files. Do not rebuild an existing repo wholesale without understanding it. Record the current state in `docs/PROJECT_STATE.md`.

Never print environment secrets or echo credential values. Do not create cloud resources, run `terraform apply`, change billing settings, destroy resources, or deploy publicly without my explicit authorization. You may create infrastructure code, scripts, docs, and workflows, and run safe local validation.

After inspection, briefly report what you found and the sequence you will follow, then proceed. Do not ask me to decide routine technical details you can reasonably resolve. Ask only when blocked or when an action is destructive/paid and requires my approval.

## 3. Product Requirements Document (PRD)

Create `docs/PRD.md` with problem, personas, user journeys, requirements, non-functional requirements, non-goals, acceptance criteria, and measurable success criteria. Keep all other documents consistent with it.

### Users
- **Integration administrator:** configures connections, schemas, transformations, retry settings, and integration state.
- **Operations engineer:** monitors events, investigates failures, retries/replays events, and checks health.
- **Viewer/reviewer:** inspects configuration/history without modifying it.
- **Source developer:** sends a signed webhook or uses the demo simulator.

This is a portfolio/demo project, not a commercial SaaS launch. Do not invent customers, production usage, or business metrics. Provide a seeded/demo workspace and repeatable examples.

### Inputs
1. HTTP webhook requests carrying JSON events.
2. Optional HMAC signature and source event identifier.
3. Integration config: source/destination, input/output schema, mappings, transformations, filters, retry policy.
4. Built-in simulator to generate known events and failures.
5. Operator actions from the UI: configure, pause/resume, inspect, retry/replay, request an investigation.

### Outputs
1. Validated/transformed payload delivered to the destination.
2. Persisted event record and state history.
3. Delivery attempts, destination status/response summary, duration, retries, and failure classification.
4. Dead-lettered events and safe replay operations.
5. Dashboard with real metrics and recent events/failures.
6. Optional AI report with evidence links, likely cause, uncertainty, and recommendations.
7. Structured logs, health/readiness endpoints, OpenAPI docs, automated CI/CD results.

### Functional requirements

**FR-01 — Integration configuration**
- Create, view, update, pause, resume, and disable integrations.
- Each includes source/destination connection, schema, versioned mappings/rules, filters, retry config, and status.
- Preserve configuration revisions; every event records the version used.
- Validate before activation and return actionable errors.

**FR-02 — Adapters**
- Define clear source/destination interfaces so new connectors do not require rewriting core processing.
- Implement generic JSON webhook source, generic HTTP destination, local/demo source simulator, and a built-in demo destination.
- HTTP destination config supports method, allowlisted headers, timeout, auth reference, and mapping.
- Do not integrate lots of vendor-specific systems in v1; generic adapters and the demo service suffice.

**FR-03 — Ingestion**
- Validate content type, payload size, JSON, source config, and configured HMAC signature.
- Persist accepted event and outbox record transactionally before acknowledging receipt.
- Return promptly; deliver asynchronously.
- Deduplicate using stable source/event identity and a database unique constraint.
- Use correlation IDs across request, event, delivery attempt, and logs.

**FR-04 — Deterministic transformation**
- Support a restricted declarative DSL for field/path mapping, rename, documented type/date conversion, defaults, required fields, allowlisted filters, and bounded nested-field access.
- Validate transformed output against the destination schema.
- Never `eval`, run uploaded Python/JS, or execute arbitrary expressions.
- Keep transformations deterministic and independently unit-testable.

**FR-05 — Async processing**
- Use GCP Pub/Sub in cloud and a Pub/Sub emulator or tested local queue adapter locally.
- API and worker must have separate entrypoints/deployments while sharing one well-structured codebase.
- Persist state transitions and attempt history.
- Distinguish queue/consumer redelivery from business-level destination retries; avoid double-retry loops.
- Acknowledge a message only after its processing outcome is safely persisted. Return non-success for transient worker failures that should be redelivered.

**FR-06 — Retry/dead letter/replay**
- Classify validation errors, permanent 4xx/auth errors, 408/429, 5xx, transport failures, and timeouts.
- Retry only eligible failures, using bounded exponential backoff with jitter and an attempt ceiling.
- Store `next_attempt_at`, attempt count, last error, and status.
- Exhausted events enter a dead-letter state/topic with a visible reason.
- Replay is an authorized, audited operation with new replay lineage; never erase old attempts.
- Use destination idempotency keys where supported. Promise **at-least-once delivery with deduplication/idempotency protections**, not universal exactly-once external side effects.

**FR-07 — Operations UI**
- Responsive dashboard; integrations list/detail/editor; event explorer; event detail; attempt timeline; failure/dead-letter view; replay action; audit history; investigation view; settings/connections by role.
- Support pagination, filters, clear loading/empty/error states, and actionable validation feedback.
- Show original/transformed JSON readably with sensitive-field redaction.
- Dashboard values must come from backend queries, not fake hardcoded counters. Seeded data must be labelled demo data.

**FR-08 — Auth/RBAC**
- Add secure authentication and workspace-level separation.
- Roles: Administrator, Operator, Viewer. Enforce permissions in the backend, not only the UI.
- Use a vetted password-hashing/session approach; do not invent cryptography.
- Provide local seed-admin setup without committed credentials. Production credentials come from environment/secret manager and can be rotated.
- Use explicit CORS allowlists. If cookies are cross-origin, configure Secure/HttpOnly and CSRF protection.

**FR-09 — Audit**
Audit integration/config changes, connection changes, permission changes, retries/replays, and AI recommendation approval. Store actor/action/entity/time/safe metadata; never log secrets or full sensitive payloads.

**FR-10 — Agentic AI operations assistant**
- Only invoke when an operator asks to investigate an event. Never call an LLM for every normal event.
- Provide an MCP server with read-only, workspace-scoped tools for event details, delivery attempts, transformation errors, destination response summary, config version, and recent successful examples.
- A `validate_proposed_mapping` tool validates a mapping without applying it.
- Return validated structured output: observed facts, evidence references, likely cause, confidence/uncertainty, recommendations, missing evidence.
- Configure at least two provider slots with routing/fallback. Use LiteLLM or lightweight provider adapters if justified; keep domain code provider-agnostic.
- Local development supports Ollama. In cloud, hosted inference is optional and disabled without explicit credentials/free quota. Always provide a deterministic non-AI diagnostic fallback.
- AI cannot directly update live integrations, change secrets, execute shell commands, call arbitrary URLs, or replay events. Human approval is mandatory for state-changing actions.
- Treat payloads/responses as untrusted prompt-injection content. Bound context and redact data before model calls. Store investigation, evidence refs, provider/model, duration, outcomes, and available token/cost stats safely.

**FR-11 — Observability**
- `/healthz` and `/readyz` with documented semantics.
- Structured logs/correlation IDs across API, outbox, Pub/Sub, worker, destination, investigation.
- Track received/succeeded/failed events, delivery duration, attempt count, dead-letter/retry counts, oldest pending outbox/event age, and AI invocation failures/latency.
- Avoid paid APM/custom monitoring as a requirement; document free-tier cost.

### Non-functional requirements

- Secure defaults, explicit authorization, least privilege, no Git secrets.
- Payload limits, timeouts, bounded retries, bounded agent steps/token spend, cancellation where reasonable.
- Transactional state changes and idempotent processing where meaningful.
- Versioned, repeatable database migrations.
- OpenAPI, consistent errors, documented pagination.
- Core domain logic testable without GCP or a live model.
- No service/dependency without a real need; app stays useful without AI.

### Acceptance criteria

Document and test these journeys:
1. Create/validate/activate an integration.
2. Send webhook → persist → queue → transform → deliver → view result.
3. Repeat the same event and verify deduplication.
4. Send malformed payload and see actionable validation failure with no delivery.
5. Simulate timeout/429/500 and verify retry and attempt history.
6. Exhaust attempts and dead-letter the event.
7. Replay as an authorized operator while preserving history.
8. Prove Viewer cannot perform restricted API actions directly.
9. Simulate worker/publish failure and verify durable recovery.
10. Run an investigation and verify evidence references; prove the agent cannot mutate integration state.
11. Run locally without cloud credentials and without an LLM API key.
12. Build/deploy through protected workflow and smoke-test when deployment credentials are configured.

### Non-goals
- Customer billing, commercial SaaS launch, marketing site, or real customer claims.
- Generic no-code workflow builder or arbitrary user-code execution.
- Universal exactly-once side effects.
- Another generic scheduler/DAG/worker-claiming engine.
- Kubernetes, multi-cloud support, GPUs, custom model training, vector database/RAG, or dozens of vendor integrations.
- Approval-free AI mutation of production systems.

## 4. Architecture requirements

Create `docs/architecture/overview.md`, `docs/architecture/data-model.md`, `docs/architecture/reliability.md`, `docs/architecture/security.md`, and `docs/architecture/algorithms.md`. Add ADRs under `docs/architecture/decisions/`.

### 4.1 Architectural shape

Use a **modular monolith codebase with independently deployable API and worker entrypoints**. Do not create a swarm of microservices. Keep domain/application boundaries explicit.

Logical components:
- **Web UI:** React + TypeScript + Vite.
- **API/control plane:** Python + FastAPI + Pydantic.
- **Domain/application layer:** integration configuration, schemas, transformations, state transitions, authorization, audit.
- **Persistence:** PostgreSQL + SQLAlchemy + Alembic.
- **Messaging adapter:** Pub/Sub in cloud; Pub/Sub emulator or tested local adapter in development.
- **Outbox dispatcher:** safely claims pending outbox records and publishes them. A single Cloud Scheduler job may trigger it only after its current free-tier allowance is verified; otherwise document a sound alternative.
- **Worker entrypoint:** accepts Pub/Sub push messages, runs deterministic validation/transformation, delivers through destination adapters, and persists outcomes.
- **Adapters:** generic webhook source, generic HTTP destination, demo source/destination. Avoid long `if/else` chains.
- **MCP/AI module:** read-only tools and a bounded investigator, sharing application services rather than duplicating business logic.
- **Observability:** structured logs, correlation IDs, health/readiness, useful operational queries.

### 4.2 Event lifecycle and reliability

Define and implement a finite state machine with permitted transitions, state owner, transactional boundaries, and recovery behavior. Suggested states (refine if justified):
`RECEIVED`, `VALIDATION_FAILED`, `PENDING_PUBLISH`, `QUEUED`, `PROCESSING`, `RETRY_SCHEDULED`, `DELIVERED`, `DEAD_LETTERED`, `REPLAY_REQUESTED`.

Do not allow arbitrary state changes scattered through endpoints. Centralize transitions in domain/application services.

Guarantee **at-least-once processing**, with database uniqueness, transactional state transitions, safe attempt logging, stable destination idempotency keys when supported, and explicit handling of duplicate events. Document the ambiguous case where a remote destination acts but its response is lost.

Use a **transactional outbox** for DB-to-Pub/Sub durability. The event and pending outbox message must commit in one transaction. Outbox claiming must handle concurrent dispatchers and crashes/leases. Never mark a message published before the broker accepts it. Failed publication remains recoverable.

Separate retry responsibility:
- Pub/Sub redelivery handles worker/consumer/infrastructure failures.
- Application retry policy handles destination outcomes and stores `next_attempt_at`.
- Poison messages and failures are bounded; do not create immediate, endless retry loops.

### 4.3 Suggested layout

Adapt if the existing repo already has a sensible structure. If empty, prefer:

```text
.
├── apps/web/                  # React + TypeScript + Vite
├── services/api/              # FastAPI control plane/webhooks
├── services/worker/           # Pub/Sub push processing entrypoint
├── packages/relay_core/       # domain, schema, transformation, policy
├── tests/{unit,integration,contract,e2e,security}/
├── evals/                     # deterministic AI investigation fixtures
├── infra/gcp/                 # Terraform and deployment config
├── scripts/                   # seed/demo/local helper scripts
├── docs/
│   ├── PRD.md
│   ├── PROJECT_STATE.md
│   ├── design/
│   ├── architecture/
│   ├── quality/
│   └── deployment/
├── .claude/rules/
├── .github/workflows/
├── CLAUDE.md
├── docker-compose.yml
├── Makefile                   # or one documented task runner
└── README.md
```

Keep package/import structure valid and do not invent empty abstraction layers.

### 4.4 Deployment target and recurring $0 goal

Target **$0/month under current free allowances at low demo usage**, without relying on introductory credits. Verify official pricing and limits at implementation/deployment time. Record source links and date checked in `docs/deployment/free-tier-cost-model.md`.

Preferred architecture if currently free-tier compatible:
- Static frontend: Cloudflare Pages free tier.
- API and worker: Cloud Run request-based billing, minimum instances zero, conservative maximum instances/resource limits.
- Queue: Google Cloud Pub/Sub, minimal retention, dead-letter topic.
- PostgreSQL: Neon free plan rather than Cloud SQL.
- Container registry: Artifact Registry only while within its current free storage/transfer allowance; configure cleanup.
- CI/CD: GitHub Actions, OIDC / Workload Identity Federation for GCP deployment, no long-lived service account JSON key.
- Secrets: Secret Manager only within verified allowance and a small number of active versions, or secure platform environment secrets where appropriate.
- Scheduler: at most one Cloud Scheduler job if needed for outbox publishing, after verifying account-level free allowance.
- Logging: Cloud Logging within current free ingestion/retention limits. Avoid paid APM and custom metrics.
- LLM: local Ollama for zero-API-cost model testing; hosted inference optional and disabled absent explicit credentials/free quota.

Official pages to verify, rather than blindly trust forever:
- Cloud Run: https://cloud.google.com/run/pricing
- Pub/Sub: https://cloud.google.com/pubsub/pricing
- Artifact Registry: https://cloud.google.com/artifact-registry/pricing
- Cloud Scheduler: https://cloud.google.com/scheduler/pricing
- Cloud Build (if used): https://cloud.google.com/build/pricing
- Secret Manager: https://cloud.google.com/secret-manager/pricing
- Google Cloud Free Program: https://cloud.google.com/free
- Neon: https://neon.com/pricing
- Cloudflare Pages: https://developers.cloudflare.com/pages/platform/limits/
- GitHub Actions billing: https://docs.github.com/en/billing/concepts/product-billing/github-actions

Do not promise the entire architecture is guaranteed free. Many GCP services require a billing account; quotas can change; egress, traffic, retained messages, image storage, and usage beyond the allowance can cost money. Budget alerts are not hard spending caps. Do not count trial credits as recurring free. Document a teardown procedure.

**Cost controls:**
- Cloud Run min instances = 0; conservative max instances, CPU/memory, timeouts, concurrency, and request body limits.
- No Cloud SQL, Memorystore/Redis, GKE/Kubernetes, VMs, NAT/VPC connectors, load balancers, paid monitoring/APM, GPUs, or mandatory paid inference.
- Avoid retained acknowledged Pub/Sub messages or large backlogs that can add storage cost.
- Configure Artifact Registry cleanup and keep image size/retention low.
- Add billing alerts where possible, and explicitly state they are not caps.
- Provide a documented Terraform destroy/teardown workflow, but require explicit confirmation before destroying a live environment.
- Protect public webhook endpoints with signature configuration, payload limits, rate limits, and minimal output.
- Provide local/test/cloud profiles. Local runs must work without GCP credentials or billing.

The cloud profile may require billing to be enabled even when usage is within free allowances. Local mode must never require cloud credentials.

## 5. Data model and algorithms

Document an ER diagram (Mermaid is fine), table purposes, constraints, indexes, and payload retention/redaction in `docs/architecture/data-model.md`. Implement migrations for these concepts, refining names if justified:
- `users`
- `workspaces`
- `workspace_memberships`
- `connections` (safe config and secret reference, not raw credential)
- `integrations`
- `integration_versions`
- `events`
- `outbox_messages`
- `delivery_attempts`
- `replay_operations` or explicit replay lineage
- `investigations`
- `audit_logs`
- `sessions` or equivalent secure session store

Use foreign keys, unique constraints, indexes, timestamps, workspace scoping, and safe JSON payload storage. Query indexes must support workspace, integration, status, time, and ID filters. Prefer cursor pagination with stable composite ordering (e.g. `(created_at, id)`) if appropriate.

### Algorithms/data structures required because they solve actual problems
1. **Hash-based deduplication:** stable source/event identity and a DB unique constraint. An in-memory set/dictionary may optimize but must never be correctness boundary across service instances.
2. **Finite-state machine:** centralizes allowed event transitions and guards against inconsistent state changes.
3. **Transactional outbox claim/lease:** use PostgreSQL locking/lease fields; if concurrent dispatchers are possible, use a safe pattern such as `FOR UPDATE SKIP LOCKED` where appropriate. Explain concurrency and crash recovery.
4. **Exponential backoff with jitter:** bounded formula, attempt ceiling, transient/permanent classification, injectable randomness for deterministic tests.
5. **Adapter registry:** dictionary/map from connector type to adapter implementation.
6. **Transformation interpreter:** bounded JSON-tree/path traversal with a whitelist of operations and explicit missing-field/type errors, never an arbitrary expression runtime.
7. **Cursor pagination:** deterministic composite cursor and proper composite indexes.
8. **Rate limiting:** implement only if correct across multiple Cloud Run instances. Do not pretend a process-local token bucket is a global limit. Prefer a documented DB-backed/windowed policy or a simple justified limit; do not introduce Redis just for rate limiting.
9. **Idempotent mutation:** unique keys and transactional checks for ingestion/replay and attempt creation.

In `docs/architecture/algorithms.md`, explain each algorithm's purpose, complexity where meaningful, consistency/concurrency trade-offs, and what happens under restarts. Do not add algorithms merely to decorate a resume.

## 6. API contract

Create `docs/design/api-contracts.md`. Use a consistent versioned REST prefix such as `/api/v1`, except webhook endpoints and protected internal callbacks. Publish OpenAPI and standardize error responses.

Suggested API surface, adjusted for consistency:
- `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- CRUD/list for `/api/v1/workspaces`, `/api/v1/integrations`, `/api/v1/connections`
- `POST /api/v1/integrations/{id}/validate`
- `POST /webhooks/{connection_id}`
- `GET /api/v1/events` with status/integration/time filters and cursor pagination
- `GET /api/v1/events/{id}` and `GET /api/v1/events/{id}/attempts`
- `POST /api/v1/events/{id}/replay` with idempotency and audit behavior
- `POST /api/v1/events/{id}/investigations`, `GET /api/v1/investigations/{id}`
- `GET /api/v1/dashboard/summary`
- `GET /healthz`, `GET /readyz`
- protected internal Pub/Sub push and outbox-dispatch endpoints with service authentication

Use correct HTTP statuses, typed schemas, consistent error codes, bounded pagination, authorization, and safe errors that do not leak DB internals or sensitive destination response bodies.

## 7. UI/UX design

Create `docs/design/ui-ux.md` before finalizing the frontend. This must look like a practical developer/operations console, not an AI marketing page.

### Navigation
Overview; Integrations; Events; Failures/Dead letters; Investigations; Audit log; Settings/Connections (role-dependent).

### Screens
1. **Overview:** real event totals, success/failure, recent failures, integration health, time filters. Trend chart only if meaningful.
2. **Integrations list/detail:** status, connector types, last activity, success/failure counts; configuration revision/history.
3. **Integration editor:** source, destination, schemas, mappings, filters, timeout/retry settings, validation/test preview, pause/resume.
4. **Event explorer:** search/filter/paginate, status and timestamps.
5. **Event detail:** redacted original payload, transformed payload, attempt timeline, destination response, selected config version, correlation IDs, authorized replay.
6. **Recovery view:** dead-letter reason, attempts, last failure, explicit replay confirmation.
7. **Investigation:** facts/evidence, likely cause, uncertainty, recommendations, provider/model and timing metadata. Never label AI hypotheses as verified facts.
8. **Audit log:** actor, action, resource, time, safe metadata.

### Visual system
Use consistent spacing/typography, neutral backgrounds, limited semantic colors, readable tables/details, responsive layouts, accessible contrast, keyboard navigation, focus states, and clear loading/empty/success/error/permission-denied states. Do not invent counters or percentages. Mark seeded content as demo data. Use reusable components; avoid needless animation or oversized hero cards.

## 8. Security and privacy

Create `docs/architecture/security.md` and a practical threat model.
- Verify webhook HMAC safely with constant-time digest comparison.
- Enforce body-size limits, content-type checks, timeouts, and sensible rate limits.
- Enforce authentication, workspace isolation, and roles in the backend.
- Store secrets only in Secret Manager/protected environment config; DB stores references only.
- Defend generic HTTP destination URLs against SSRF: reject loopback/private/link-local/multicast/cloud-metadata addresses, resolve DNS safely, validate each redirect or disable redirects by default, consider DNS rebinding, and allow local test targets only under explicit dev config. Document remaining limits.
- Restrict destination methods and headers; do not forward inbound Authorization/Cookie headers by default.
- Redact sensitive values in logs, payload views, response summaries, audits, and AI context.
- Treat payloads/responses as untrusted prompt-injection content. Bound context. No arbitrary filesystem/shell/URL-fetch/database-query MCP tools.
- Use least-privilege workload identities and GitHub OIDC/Workload Identity Federation. No GCP JSON keys in repository secrets.
- Use lockfiles, security scanning, pinned GitHub Actions where practical, and SBOM generation if feasible.
- Never commit `.env`, API/model keys, session secrets, cloud credentials, or live customer data.

## 9. Testing and quality gates

Create `docs/quality/testing-strategy.md` and implement meaningful test suites.

Suggested tools:
- Python: `pytest`, `pytest-cov`, `httpx`, `Hypothesis`, `ruff`, `mypy` (or pyright if justified).
- Frontend: TypeScript strict mode, ESLint, Vitest/Testing Library, Playwright for key end-to-end journeys.
- Database: PostgreSQL in Docker/service containers and migration tests against a clean DB.
- Messaging: Google Pub/Sub emulator locally if practical; otherwise a small queue abstraction plus contract tests, while documenting anything that remains cloud-only/unverified.

Required tests include:
- Transformation correctness, type mismatches, missing required fields, and invalid mappings.
- Duplicate/concurrent event submissions and idempotency.
- All allowed and forbidden state transitions.
- Outbox publish success/failure, DB transaction boundaries, expired leases, concurrent claims, and crash recovery.
- Retry classification and bounds; timeouts, 429, permanent 4xx, 5xx, malformed responses, and connection errors.
- Replay lineage and audit history.
- Workspace/role authorization enforced by direct API calls.
- Webhook signature correctness and invalid-signature rejection.
- SSRF URL validation, redirects/DNS edge cases where testable.
- Payload size validation and secret redaction.
- AI tool access boundaries, malformed model output, timeout/fallback, provider-unavailable fallback, and evidence-reference validity.
- UI loading/empty/error/permission states and one end-to-end successful and failed event path.

CI gates should include formatting, lint, type checks, unit/integration tests, frontend build, migration checks, dependency/security checks, and a core-logic coverage threshold (target 80% initially). Prefer meaningful tests to coverage padding. Mock LLMs in normal CI; live model evaluation should be optional or separately scheduled.

## 10. CI/CD, infrastructure, and deployment

Create `.github/workflows/` workflows for:
- Pull-request verification: lint, formatting, type checks, tests, security/dependency checks, frontend build.
- Main-branch build/push and staging deployment if cloud configuration is available.
- Protected/manual production deployment with explicit approval.
- Post-deployment smoke tests and a documented rollback procedure.

Failed checks must block deployment. Promote the same immutable image digest between environments instead of rebuilding per environment.

Use Terraform for only the necessary GCP resources: required APIs, service identities/IAM, Pub/Sub topics/subscriptions/dead-letter path, Cloud Run API/worker, optional single outbox scheduler, Artifact Registry, and secret references. Avoid services not required by the architecture. Verify prices and quotas first.

Use GitHub OIDC/Workload Identity Federation. Store non-secret deployment configuration as variables and use secrets only for genuinely secret values.

**Cloud safety:** Do not execute `terraform apply`, `gcloud` commands that create/update/delete live resources, or a public production deployment unless I explicitly ask and credentials are available. You may format/validate Terraform and prepare deployment instructions. Never expose account IDs, sensitive state, or secret values in outputs.

## 11. Persistent project instructions and memory

Create and maintain these files. Keep `CLAUDE.md` concise enough to be loaded regularly; move detailed policies to scoped rules and link them.

### `CLAUDE.md`
Include the mission/positioning, architecture summary, links to PRD/architecture/design/testing/security/cost docs, canonical local commands, non-negotiable security/cost rules, the requirement that normal processing works without LLMs, prohibition on unapproved cloud changes, and instructions to update project state/memory.

### `.claude/rules/`
Create scoped rules, using the installed Claude Code's supported rules syntax (including path scoping if supported):
- `backend.md`: Python/FastAPI typing, domain/application boundaries, migrations, errors.
- `frontend.md`: strict TypeScript, reusable components, accessible UX, no fake stats.
- `testing.md`: test layers, deterministic tests, commands, coverage policy.
- `security.md`: secrets, auth/RBAC, webhook signatures, SSRF, redaction, MCP boundaries.
- `cloud-deployment.md`: current free-tier verification, no unapproved apply/deploy/destroy, least privilege, OIDC, rollback.
- `documentation.md`: PRD/ADR/state updates and factual claims.

### `docs/PROJECT_STATE.md`
This is the **committed, portable source of truth for project memory**. Keep updated with:
- current phase and completed capabilities;
- actual architecture and key decisions;
- tested local setup/commands;
- test suites and last verified status/date if known;
- required environment variable *names* (never secret values);
- known bugs/limitations and unverified cloud assumptions;
- next tasks in priority order;
- blockers and required credentials.

### ADRs
Create `docs/architecture/decisions/ADR-0001-project-shape.md` and more ADRs only for meaningful choices: API/worker modular monolith, messaging/outbox semantics, destination transformation DSL, free-tier deployment, and AI/MCP boundaries. Include context, decision, alternatives, consequences, and date.

### Claude Code auto-memory
If native auto-memory is enabled, use it only for durable, reusable findings not obvious from checked-in docs. Do not store credentials, PII, transient task notes, or duplicate large documents. `CLAUDE.md`, `.claude/rules/`, and `docs/PROJECT_STATE.md` remain the portable source of truth for the repo.

## 12. Coding/collaboration rules (mandatory)

1. Understand the surrounding code and docs before editing.
2. For multi-step changes, outline a short plan, then execute it; do not answer with only a plan.
3. Make small coherent changes. Avoid giant rewrites.
4. No fake features: buttons must work or be explicitly disabled; no fake dashboard metrics, no finished-sounding TODOs.
5. No fake verification: run commands and report true results. State why an unrun test could not run.
6. For fast-changing SDKs, MCP, provider APIs, cloud syntax/pricing, verify official docs when online; do not guess current signatures.
7. Add dependencies only when justified; keep versions/lockfiles consistent. No blanket dependency upgrades.
8. Catch meaningful exceptions, log correlation IDs, return safe errors, and never swallow failures silently.
9. Never write secrets/sensitive payloads to logs or send them into model context.
10. Never use `eval`, unsafe deserialization, shell-from-user-input, or arbitrary destination URLs without SSRF defenses.
11. Every retry, queue redelivery loop, and agent loop must be bounded and observable.
12. Avoid needless abstraction. Do not add Kubernetes, Redis, a vector DB, or model training without a concrete requirement and a cost-viable plan.
13. Update docs and `docs/PROJECT_STATE.md` when behavior, architecture, commands, environment variables, tests, or known limitations change.
14. No destructive operations, force pushes, production deployments, Terraform apply, or resource destruction without explicit permission.
15. Record meaningful architecture trade-offs as ADRs.
16. Make the UI accessible and provide real error/empty/loading states.
17. Final report must summarize implemented work, important files, tests actually run, local run instructions, cloud/cost status, unresolved issues, and any decision that requires me.

## 13. Required deliverables

By portfolio review, the repository should contain:
- Working frontend/backend and documented local setup.
- Source/destination adapters and demo scenarios.
- Durable ingestion, validation/transformation, delivery attempts, retries, dead-letter, replay, event history, and auditability.
- Authentication and backend-enforced workspace/role permissions.
- Read-only MCP tools and optional AI investigation workflow with provider fallback and deterministic no-AI fallback.
- PostgreSQL schema/migrations.
- Docker Compose local environment and seed/demo scripts.
- Automated test suites and CI gates.
- GCP Terraform/deployment configuration that does not auto-apply live resources.
- README, PRD, architecture/data/reliability/security/algorithms docs, API contracts, UI/UX spec, testing strategy, free-tier cost model, teardown instructions, `CLAUDE.md`, `.claude/rules/`, and `docs/PROJECT_STATE.md`.
- A recruiter-friendly demo walkthrough that reproduces success and failure/recovery paths.

## 14. Implementation sequence — proceed through all stages

**Stage A — Inspect and specify**
- Inspect repo and preserve all existing work.
- Record baseline in `docs/PROJECT_STATE.md`.
- Create PRD, architecture overview, data model, reliability/security, algorithms, UI/UX, API contract, testing strategy, free-tier cost model, and initial ADRs.
- Keep documents consistent and dependency choices justified.

**Stage B — Foundation**
- Set up formatter, lint/type checks, configuration validation, structured logs, correlation IDs, Docker/Compose, migrations, and CI.
- Implement domain models, schema/config validation, and event state machine.

**Stage C — Core vertical slice**
- Implement auth/RBAC, integration configuration, webhook ingestion, deduplication, transactional outbox, local queue adapter/emulator, worker processing, transformations, demo destination, delivery attempts, and event UI.
- Add failure cases along the way.

**Stage D — Reliability and operations**
- Add retry policy, dead-letter behavior, replay, audit, URL safety, dashboard queries, observability, and recovery tests.

**Stage E — Agentic assistant**
- Implement MCP tools and bounded investigator over persisted data.
- Add provider config/fallback, output validation, local Ollama option, redaction, timeouts, evaluation fixtures, and deterministic fallback.

**Stage F — Cloud and delivery**
- Complete infrastructure code, GitHub Actions gates, OIDC deployment docs, staging/manual production flow, smoke tests, rollback/runbook, cost verification, and teardown docs.
- Do not provision/deploy until I explicitly approve and credentials are available.

**Stage G — Final verification**
- Run backend lint/type/tests, frontend lint/build/tests, migration tests, and end-to-end tests where possible.
- Review setup from a clean-checkout perspective. Verify env docs and `.env.example` contain no secrets.
- Verify UI flows use real backend data and the demo is reproducible.
- Update `docs/PROJECT_STATE.md` and report only verified facts.

## 15. Definition of done

Relay is ready for portfolio review when:
- Its problem, architecture, design decisions, and local setup are clear from documentation.
- It runs locally without cloud billing/credentials and without an LLM API key.
- The deterministic integration path works from source event to transformed destination payload and visible result.
- Failure/retry/dead-letter/replay behavior is tested and preserves audit history.
- Core behavior is covered by automated tests and CI quality gates.
- Security-critical paths have tests and documented limits.
- AI investigations use real persisted evidence and cannot mutate state without human approval.
- Cloud deployment has verified free-tier assumptions, a cost model, and teardown steps; no unnecessary paid services are required.
- No docs/resume text claim unmeasured performance, accuracy, savings, usage, or deployment success.

**Begin now by inspecting the repository. Then create the docs and implement Relay in coherent, tested increments. Do not stop after returning a plan.**
