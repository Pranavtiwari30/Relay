# Project state

The committed source of truth for where Relay stands. Update it whenever behaviour, architecture,
commands, environment variables, tests or known limits change.

**Last updated:** 2026-10-11

## Baseline

On 2026-10-11 the repository contained one file, `Relay_Claude_Code_Master_Prompt.md`, and was not
a Git repository. Everything else was built from that brief. The owner committed it as `f118d55`
and pushed to `github.com/Pranavtiwari30/Relay` the same day.

## Phase

**Implementation complete for v1 scope; pre-deployment.** Stages A–E and G of the brief are done.
Stage F (cloud) is done as code — infrastructure, workflows, runbook, cost model — and has
deliberately **not** been applied.

## Completed capabilities

| Requirement | State | Notes |
|---|---|---|
| FR-01 Integration configuration | Done | CRUD, immutable versions, validate, activate / pause / resume / disable. |
| FR-02 Adapters | Done | Webhook source; HTTP and demo destinations; registry; demo simulator. |
| FR-03 Ingestion | Done | Size/type/JSON/HMAC checks, transactional event + outbox, dedup, correlation ids. |
| FR-04 Transformation | Done | Bounded DSL, 11 conversions, 11 filter operators, restricted JSON Schema. |
| FR-05 Async processing | Done | Separate API and worker; Pub/Sub publisher; tested local queue. |
| FR-06 Retry / dead-letter / replay | Done | Classification, backoff + jitter, lease recovery, audited idempotent replay. |
| FR-07 Operations UI | Done | All screens in the PRD. |
| FR-08 Auth / RBAC | Done | Argon2id, server-side sessions, CSRF, three roles, workspace isolation. |
| FR-09 Audit | Done | Config, connection, member, replay, investigation and decision events. |
| FR-10 Investigation assistant | Done | 7 read-only tools, MCP server, 3 provider adapters, deterministic fallback. |
| FR-11 Observability | Done | Health/readiness, JSON logs, correlation ids, dashboard figures. |

Acceptance criteria AC-1 to AC-11 have passing automated tests. **AC-12 (protected deploy with a
smoke test) is not met**: it needs cloud credentials.

## Architecture as built

- Modular monolith: `packages/relay_core` (domain, db, adapters, services, ai), `services/api`,
  `services/worker`, `apps/web`.
- 18 tables, one Alembic migration (`0001`), reversible, no model/migration drift.
- Event states: `RECEIVED`, `HELD`, `VALIDATION_FAILED`, `FILTERED`, `PENDING_PUBLISH`, `QUEUED`,
  `PROCESSING`, `RETRY_SCHEDULED`, `DELIVERED`, `DEAD_LETTERED`, `REPLAY_REQUESTED`. `HELD` and
  `FILTERED` were added to the brief's suggested set (paused integrations; filter rules).
- Guarantee: at-least-once with idempotency protections.

### Decisions (ADRs)

| ADR | Decision |
|---|---|
| 0001 | Modular monolith, two entry points |
| 0002 | Transactional outbox, push delivery, at-least-once, separated retry mechanisms |
| 0003 | Restricted declarative transformation DSL |
| 0004 | Cloud Run + Pub/Sub + Neon + Pages; dispatcher every 15 minutes for cost reasons |
| 0005 | Read-only, bounded, optional investigation assistant with deterministic fallback |
| 0006 | Server-side sessions behind a same-origin proxy |

### Departures from the brief, and why

| Brief | Built | Reason |
|---|---|---|
| Pub/Sub emulator locally | PostgreSQL-backed local queue | Emulator needs Java or Docker; the brief allows a tested local adapter. |
| Suggested states | plus `HELD`, `FILTERED` | Needed for pause/resume and filters. |
| `event_transitions` not listed | added | "Persist state transitions" requires a history table. |
| Secret Manager or env | `env:RELAY_SECRET_*` references only | Cloud Run injects Secret Manager values as env vars; one resolver, zero runtime API calls. |
| Delivered events replayable (implied) | not replayable | The destination confirmed them; re-sending is the duplicate Relay prevents. |
| LiteLLM or adapters | three small adapters | Avoids a large dependency; wire formats pinned by contract tests. |
| Scheduler cadence unspecified | every 15 minutes | Verified Neon free-plan compute limit (see cost model). |

## Local setup (tested)

Requirements: `uv`, Node 22+, PostgreSQL 15+ binaries on `PATH`. Docker is not required.

```bash
make setup    # install, .env with generated secrets, local Postgres on :54329, migrate, seed
make dev      # API :8000, worker :8001, UI http://localhost:5173
make check    # all gates CI runs on a PR except browser e2e and security scans
```

Other targets: `make help`. The local PostgreSQL is a project-local cluster in `.local/pgdata`,
separate from any system-wide install.

## Tests — last verified 2026-10-11 (macOS arm64, Python 3.12, PostgreSQL 16, Node 22)

| Suite | Count | Status |
|---|---|---|
| Python unit / integration / contract / security / e2e | 295 / 110 / 47 / 173 / 7 = **632** | pass |
| Web unit (Vitest) | 30 | pass |
| Browser e2e (Playwright, Chromium) | 7 | pass |
| Eval fixtures (deterministic investigator) | 9 | pass |
| `ruff format --check`, `ruff check` | — | clean |
| `mypy --strict` (78 source files) | — | clean |
| `tsc` strict, ESLint, Prettier | — | clean |
| `alembic upgrade` → `check` → `downgrade` | — | clean |
| `terraform fmt` + `validate` (Terraform 1.16.5, google provider 8.x) | — | valid |
| `actionlint` on all workflows | — | no findings |
| `scripts/smoke-test.sh` against a local API | — | pass |

Coverage: 90% of `relay_core`, 92% overall (floor 80% on `relay_core`).

Checked by hand once: HTTPS delivery with address pinning to a public host succeeds with a valid
certificate and is refused when the certificate does not match the hostname.

### First CI run — 2026-10-11, commit `f118d55`, Ubuntu

[Run 38094304293](https://github.com/Pranavtiwari30/Relay/actions/runs/38094304293) (workflow *Deploy (staging)*, which calls the CI workflow).

| Job | Result |
|---|---|
| Python — lint, types, migrations, tests, coverage floor, evals | pass |
| Web — lint, types, tests, build | pass |
| Browser end-to-end (Playwright) | pass |
| Terraform validate + build of both container images | pass |
| Dependency, secret and SBOM checks | **failed at the secret scan** (both dependency audits passed first; SBOM step did not run) |
| Build and push, deploy, web deploy | skipped by design — no cloud variables are configured |

**Cause of the failure.** gitleaks reported three findings, all synthetic test fixtures: a made-up
string in a payment provider's live-key format used by the redaction test (two lines), and the test
harness's fake webhook secret. No real credential was committed: `.env` is not tracked, and none of
the locally generated secret values appears in any tracked file.

**Fix (in the working tree; passes locally, not yet re-run in CI).**

- The fixtures were replaced with plainly synthetic values, so new commits scan clean. All 632
  tests still pass.
- The three findings already in commit `f118d55` are listed by exact fingerprint in
  `.gitleaksignore`, each with its reason. The scanner checks every commit, so without this the
  history would keep failing; the alternative is rewriting history with a force-push, which was not
  done.
- `make secret-scan` now runs the same check locally (needs `gitleaks` or Docker).
- Verified with gitleaks 8.30.1: the history scan and the pending-changes scan both report no
  leaks, and removing `.gitleaksignore` brings the three findings back.

Also noted from that run: GitHub will move `ubuntu-latest` to Ubuntu 26 from 2026-10-19. The
workflows use `ubuntu-latest`, so the image under them will change; nothing has been pinned.

## Environment variables (names only)

All are prefixed `RELAY_`. Defaults are in `packages/relay_core/relay_core/config.py`; local values
are generated into `.env` by `make setup`.

| Area | Variables |
|---|---|
| Runtime | `ENV`, `LOG_LEVEL`, `LOG_FORMAT` |
| Database | `DATABASE_URL`, `DB_POOL_SIZE`, `PAYLOAD_RETENTION_DAYS` |
| HTTP edge | `CORS_ALLOWED_ORIGINS`, `MAX_WEBHOOK_BODY_BYTES`, `MAX_API_BODY_BYTES`, `WEBHOOK_RATE_LIMIT_PER_MINUTE`, `LOGIN_RATE_LIMIT_PER_15_MINUTES`, `SIGNATURE_TOLERANCE_SECONDS` |
| Sessions | `SESSION_COOKIE_NAME`, `SESSION_TTL_HOURS`, `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE` |
| Messaging | `QUEUE_BACKEND`, `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC`, `PUBSUB_PUBLISH_TIMEOUT_SECONDS`, `INLINE_PUBLISH`, `WORKER_BASE_URL`, `BACKGROUND_LOOPS`, `DISPATCH_INTERVAL_SECONDS`, `OUTBOX_BATCH_SIZE`, `OUTBOX_LEASE_SECONDS`, `PROCESSING_LEASE_MARGIN_SECONDS`, `LOCAL_QUEUE_MAX_DELIVERY_ATTEMPTS` |
| Internal auth | `INTERNAL_AUTH_MODE`, `INTERNAL_TOKEN` (secret), `INTERNAL_OIDC_AUDIENCE`, `INTERNAL_OIDC_SERVICE_ACCOUNTS` |
| Destinations | `DESTINATION_ALLOW_HTTP`, `DESTINATION_ALLOW_PRIVATE_HOSTS` (dev only), `DESTINATION_MAX_RESPONSE_BYTES`, `DEMO_DESTINATION_BASE_URL` |
| Secret values | `SECRET_DEMO_WEBHOOK`, `SECRET_DEMO_DESTINATION_TOKEN`, and any `SECRET_<NAME>` a connection references |
| Seed (script only) | `SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD`, `SEED_OPERATOR_EMAIL`, `SEED_OPERATOR_PASSWORD`, `SEED_VIEWER_EMAIL`, `SEED_VIEWER_PASSWORD` |
| Assistant (optional) | `AI_PROVIDERS`, `AI_MAX_STEPS`, `AI_REQUEST_TIMEOUT_SECONDS`, `AI_TOTAL_TIMEOUT_SECONDS`, `AI_MAX_OUTPUT_TOKENS`, `AI_MAX_TOOL_RESULT_CHARS`, `AI_OLLAMA_BASE_URL`, `AI_OLLAMA_MODEL`, `AI_OPENAI_COMPAT_BASE_URL`, `AI_OPENAI_COMPAT_API_KEY` (secret), `AI_OPENAI_COMPAT_MODEL`, `AI_ANTHROPIC_API_KEY` (secret), `AI_ANTHROPIC_MODEL`, `AI_ANTHROPIC_EFFORT` |
| MCP | `MCP_WORKSPACE_ID` |
| Tests / tooling | `TEST_DATABASE_URL`, `E2E_DATABASE_URL`, `PGPORT`, `PGDATA`, `API_PROXY_TARGET`, `WEB_PORT`, `CAPTURE_SCREENSHOTS` |

Frontend build: `VITE_API_BASE_URL` (normally empty: same origin). Cloudflare Pages:
`RELAY_API_ORIGIN`.

## Not verified — treat as assumptions

Nothing below has run. Each is written from documentation and validated only statically.

| Area | What is unverified | First thing likely to need attention |
|---|---|---|
| **Cloud deployment** | No resource has been created. | IAM bindings and the first `terraform apply`. |
| Pub/Sub | Publisher tested against a recorded-shape fake, not the service. | Publisher permissions; push OIDC audience. |
| Worker OIDC auth | Claim checks tested with an injected verifier; Google's verification call never ran. | `custom_audiences` matching the token audience. |
| Cloud Run ingress | Worker set to internal-only. Google's documentation (read 2026-10-11) says Pub/Sub and Scheduler in the same project are admitted. | Confirm on first deploy. |
| Cloud Scheduler | Job definition unexecuted. | Auth and cadence. |
| GitHub Actions — deploy path | The verification jobs have run (see above). The build-and-push, release and web-deploy jobs have only been skipped, so their steps are still unexecuted. | Registry login, `gcloud` steps, the rollback step. |
| GitHub Actions — SBOM step | Has not run yet (it follows the secret scan, which failed). | Action inputs. |
| Containers | Both images build in CI. Neither has been started, and `docker-compose.yml` has never been run. | Start-up command and port; Compose wiring. |
| Cloudflare Pages proxy | `apps/web/functions/` unexecuted. | Routing precedence vs `_redirects`. |
| Neon | No project created; pool sizing is a guess for a serverless database. | Connection limits; cold-start latency on `/readyz`. |
| Live model | No provider called. Ollama not installed; no API key used. | Tool-calling reliability of a given local model. |
| Cost | Arithmetic from published allowances; no bill. | Neon compute hours; Artifact Registry storage. |
| Linux | Tests, builds and browser e2e pass on GitHub's Ubuntu runner. Nothing else has run on Linux. | — |

## Known limitations

- **Cloud retry latency** is bounded below by the dispatcher schedule (15 minutes by default).
  Local is one second. See ADR-0004; ADR-0002 notes the event-driven alternative.
- **Fixed-window rate limiting** admits up to twice the limit across a window boundary.
- **Resuming** an integration with very many held events re-queues them in one transaction.
- **No event ordering** guarantee between events.
- **Delivered events cannot be replayed** (by design).
- **Editor:** nested `each` mappings are edited as JSON; the form shows them read-only.
- **No** MFA, SSO, self-service password reset, bulk replay, or push-based UI updates.
- **Redaction** recognises known key names and value shapes; it is not a guarantee, and Relay is
  not designed to hold regulated data.
- **A destination that ignores idempotency keys** can receive duplicates after a lost response.
- The **transformation DSL** cannot compute (no arithmetic, concatenation or conditionals).
- `getaddrinfo` has no independent timeout; a slow resolver is bounded by the request timeout.
- **Accessibility** has automated checks only; no assistive-technology audit.
- JSONB reorders object keys, so displayed payload key order differs from what was sent.

No known functional bugs at the time of writing.

## Next tasks, in priority order

1. **Commit and push the secret-scan fix** (fixtures, `.gitleaksignore`, `make secret-scan`, docs)
   and confirm the security job, including the SBOM step, goes green.
2. **Decide whether to pin the runner image** (`ubuntu-24.04`) before `ubuntu-latest` moves to
   Ubuntu 26 on 2026-10-19, or let it float and fix what breaks.
3. **Deploy staging** following `docs/deployment/runbook.md`, then record what differed from the
   assumptions above and correct the docs.
4. **Read the first billing report** and replace the cost estimates with observed figures.
5. **Run the assistant against a live model** (`make evals` has a `--live` mode) and record results
   per model, without generalising beyond the nine fixtures.
6. Optional: event-driven retry wake-ups (Cloud Tasks) to remove the cloud latency floor; a form
   editor for nested mappings; bulk replay.

## Blockers and what the owner must provide

| Needed for | What |
|---|---|
| CI | A GitHub repository (remote). |
| Staging deploy | A GCP project with billing enabled; a Neon project; a Cloudflare account; explicit go-ahead to create resources. |
| Production deploy | Reviewers on the `production` GitHub environment. |
| Hosted model (optional) | An API key and acceptance of per-request cost. |
| Licensing | A choice of license (none is set). |
