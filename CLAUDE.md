# Relay

Relay is an **event-driven integration and reliability platform**: it ingests webhooks, validates
and transforms them with a declarative mapping, delivers them to a destination API with retries,
dead-lettering and audited replay, and helps operators diagnose failures.

It is a portfolio project aimed at SDE / forward-deployed engineering roles. **The story is
software engineering** — APIs, integration, reliability, testing, cloud operations. The AI
assistant is a small, bounded, optional feature, not the product.

**Start here:** [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md) — what exists, what is verified,
what is not, and what is next. Keep it current.

## Architecture in five lines

- Modular monolith, two entry points: `services/api` (control plane + webhooks) and
  `services/worker` (delivery + outbox dispatcher). Shared logic in `packages/relay_core`.
- `relay_core/domain/` is pure (no I/O). Application services in `relay_core/services/` own
  transactions. Dependencies point inward.
- PostgreSQL is the system of record. A **transactional outbox** hands work to the queue
  (Pub/Sub in cloud, a PostgreSQL-backed queue locally).
- Guarantee: **at-least-once delivery with idempotency protections**. Never claim exactly-once.
- Frontend: `apps/web` (React + TypeScript + Vite), same-origin with the API via a proxy.

## Commands

```bash
make setup        # first time: deps, .env with generated secrets, local Postgres, migrate, seed
make dev          # API :8000, worker :8001, UI :5173
make check        # lint + types + all Python tests with coverage + web tests + build
make test         # Python tests only        make test-unit   # no database needed
make test-web-e2e # Playwright, own stack and throwaway database
make fmt          # auto-format              make migrate     # apply migrations
make evals        # investigation eval scorecard (no model)
make tf-check     # terraform fmt + validate (needs terraform on PATH)
```

Local Postgres is a project-local cluster on port **54329** (`scripts/local-postgres.sh`), not the
system one. Run a single test with `uv run pytest tests/path::test_name`.

## Rules that are not negotiable

1. **Event status changes only through `services/event_state.transition()`.** A static test fails
   the build otherwise. Add new states and edges in `domain/states.py` and update the edge list in
   `tests/unit/test_domain_rules.py`.
2. **Event processing must work with no model provider.** Never call an LLM on the ingest or
   delivery path. The investigation assistant is read-only and human-approved.
3. **No cloud changes without explicit permission.** Do not run `terraform apply`/`destroy`, any
   mutating `gcloud` command, or a deploy. Writing and validating infrastructure code is fine.
4. **No secrets** in Git, logs, error messages, the database (references only: `env:RELAY_SECRET_*`),
   or model context. Never print values from `.env`.
5. **No fake anything.** No hardcoded dashboard numbers, no buttons that do nothing, no claim that
   a test, benchmark or deployment succeeded unless it was run. Say what was not verified.
6. **Every loop is bounded and observable:** retries, broker redelivery, agent steps.
7. **Do not call arbitrary URLs.** Outbound requests to operator-supplied hosts go through
   `HttpDestinationAdapter` and its SSRF checks.
8. **Cost discipline.** No Redis, Kubernetes, Cloud SQL, VPC connector, vector database or paid
   monitoring without a concrete requirement and an ADR. Re-verify provider pricing before changing
   the cost model.

## When you change things

- Behaviour, architecture, commands, environment variables, tests or known limits changed →
  update `docs/PROJECT_STATE.md` and the affected doc.
- A meaningful trade-off → an ADR in `docs/architecture/decisions/`.
- Schema changed → a new Alembic migration with a working downgrade; `alembic check` must be clean.
- API changed → `make openapi` and update `docs/design/api-contracts.md` if conventions changed.

## Documentation map

| Topic | Document |
|---|---|
| Requirements, acceptance criteria | [docs/PRD.md](docs/PRD.md) |
| Components, event life cycle | [docs/architecture/overview.md](docs/architecture/overview.md) |
| State machine, outbox, retries, failure modes | [docs/architecture/reliability.md](docs/architecture/reliability.md) |
| Tables, indexes, redaction, retention | [docs/architecture/data-model.md](docs/architecture/data-model.md) |
| Threat model, SSRF, auth, assistant boundaries | [docs/architecture/security.md](docs/architecture/security.md) |
| Algorithms and their trade-offs | [docs/architecture/algorithms.md](docs/architecture/algorithms.md) |
| Decisions | [docs/architecture/decisions/](docs/architecture/decisions/) |
| API conventions | [docs/design/api-contracts.md](docs/design/api-contracts.md) |
| UI and visual system | [docs/design/ui-ux.md](docs/design/ui-ux.md) |
| Tests and what they prove | [docs/quality/testing-strategy.md](docs/quality/testing-strategy.md) |
| Cost model (verified sources) | [docs/deployment/free-tier-cost-model.md](docs/deployment/free-tier-cost-model.md) |
| Deploy, rollback, teardown | [docs/deployment/runbook.md](docs/deployment/runbook.md) |
| Demo script | [docs/demo-walkthrough.md](docs/demo-walkthrough.md) |

Detailed, path-scoped rules are in [.claude/rules/](.claude/rules/).
