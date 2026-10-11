# Testing strategy

## What the tests are for

Relay's value is a set of guarantees: an acknowledged event is not lost, a duplicate is not
processed twice, a viewer cannot replay, the assistant cannot write. A guarantee that is not tested
is a hope. The suite is organised around proving those guarantees, and around failing loudly when
one stops holding.

Three rules shape it:

1. **Test against the real thing where the real thing is the point.** Reliability tests run on real
   PostgreSQL (locks, unique constraints and `SKIP LOCKED` cannot be faked) and deliver to a real
   HTTP server on loopback.
2. **Make time and randomness inputs.** Services take a clock and a random source, so retry
   schedules and lease expiry are tested deterministically, without sleeping.
3. **A test that cannot fail is not a test.** Guards are checked against a deliberate violation;
   vacuous assertions are removed.

## Layers

| Layer | Location | Needs | What it proves |
|---|---|---|---|
| **Unit** | `tests/unit` | nothing | The pure domain: transformation DSL, state machine, retry policy, pagination, roles, configuration validation, and the static guard on who may change event status. Includes property-based tests. |
| **Security (pure)** | `tests/security/test_edge_defences.py` | nothing | SSRF address and URL rules, webhook signatures, JSON Schema restrictions, redaction. |
| **Integration** | `tests/integration` | PostgreSQL | The pipeline end to end through the HTTP interfaces of both services; retries, dead-lettering, replay; outbox and lease recovery; the control plane; cross-cutting invariants; the eval fixtures. |
| **Security (stateful)** | `tests/security` | PostgreSQL | Authentication, CSRF, the role matrix, workspace isolation, and the investigation assistant's boundaries. |
| **Contract** | `tests/contract` | PostgreSQL (MCP test only) | Wire formats Relay does not control: the Pub/Sub push envelope, every registered adapter, three model-provider APIs, the MCP server. |
| **End to end** | `tests/e2e` | PostgreSQL, free ports | Two real uvicorn servers, the local queue, the background dispatcher and the demo destination over real sockets, in real time. |
| **Web unit** | `apps/web/src/**/*.test.*` | Node | Formatting rules, the API client, UI states, permission gating, chart accessibility. |
| **Browser e2e** | `apps/web/e2e` | Chromium + the stack | The main journeys through the actual UI against a throwaway database. |
| **Evals** | `evals/` | PostgreSQL | Nine investigation scenarios scored for cause, evidence validity and absence of side effects. |

### Current counts

Last full run: **2026-10-11**, on macOS (arm64), Python 3.12, PostgreSQL 16, Node 22.

| Suite | Tests | Result |
|---|---|---|
| Python unit | 295 | pass |
| Python integration | 110 | pass |
| Python contract | 47 | pass |
| Python security | 173 | pass |
| Python end to end | 7 | pass |
| **Python total** | **632** | **pass** (about 35 s) |
| Web unit (Vitest) | 30 | pass |
| Browser e2e (Playwright, Chromium) | 7 | pass |
| Eval fixtures, deterministic investigator | 9 | pass |

**Coverage:** 90% of `relay_core` (lines and branches), 92% across all three packages. CI fails
below 80% on `relay_core`. Coverage is treated as a floor that catches untested modules, not as a
target: no test was written to raise the number.

Counts are from that local run. The first GitHub Actions run (2026-10-11, Ubuntu) passed the
Python, web, browser end-to-end and Terraform/image-build jobs; its secret scan failed on three
synthetic test fixtures, since replaced. See [PROJECT_STATE.md](../PROJECT_STATE.md).

## Acceptance criteria → tests

| | Journey | Representative tests |
|---|---|---|
| AC-1 | Create, validate, activate | `test_integration_is_created_validated_and_activated`; browser: *an administrator validates, tests and saves…* |
| AC-2 | Webhook → delivered → visible | `test_event_is_persisted_queued_transformed_and_delivered`; e2e `test_success_path_from_simulated_source_to_demo_destination`; browser: *an event flows from the simulated source…* |
| AC-3 | Deduplication | `test_repeating_an_event_is_deduplicated`, `test_concurrent_duplicates_create_exactly_one_event` |
| AC-4 | Malformed payload | `test_schema_invalid_payload_is_stored_with_actionable_errors_and_never_delivered`, `test_malformed_requests_are_rejected_without_persisting` |
| AC-5 | Timeout / 429 / 500 → retry | `test_retryable_failure_is_retried_after_backoff_and_then_delivered`, `test_429_honours_retry_after…`, `test_timeout_is_retryable…`, `test_connection_failure_is_retryable`, `test_malformed_http_response_is_retryable` |
| AC-6 | Exhaustion → dead letter | `test_attempts_are_bounded_and_exhaustion_dead_letters_with_a_visible_reason` |
| AC-7 | Authorised replay, history kept | `test_operator_replay_delivers_and_preserves_history`, `test_replay_is_idempotent_on_its_key`, `test_concurrent_replays_with_different_keys_replay_once` |
| AC-8 | Viewer blocked at the API | `test_viewer_cannot_perform_any_restricted_action`; browser: *a viewer can read but cannot replay…* |
| AC-9 | Publish failure / worker crash | `test_publish_failure_does_not_lose_the_event`, `test_dispatcher_crash_after_publish_republishes_and_worker_drops_the_duplicate`, `test_worker_crash_mid_attempt_is_recovered_when_the_lease_expires` |
| AC-10 | Evidence-backed, cannot mutate | `test_ai_investigation_reads_stored_evidence_and_cites_it`, `test_a_model_that_tries_to_mutate_state_gets_nowhere`, `test_investigation_reads_run_in_a_read_only_database_transaction` |
| AC-11 | No cloud, no model | The entire suite runs that way; `test_deterministic_diagnosis_without_any_model` |
| AC-12 | Protected deploy + smoke test | **Not exercised.** Workflows lint and Terraform validates; `scripts/smoke-test.sh` passes against a local API. No deployment has been run. |

## Techniques worth knowing about

**Property-based tests** (Hypothesis) check statements that must hold for all inputs: the
interpreter never raises and is deterministic on arbitrary JSON; backoff is always within bounds;
path parse/format round-trips; redaction is idempotent and shape-preserving; cursors round-trip.

**Concurrency tests** use real threads against real PostgreSQL and a barrier to start them
together: eight simultaneous duplicate webhooks; four dispatchers draining sixty rows; four
concurrent replays of one event.

**Crash simulation.** The worker's three steps are separable, so a test runs the claim step and
then stops — exactly the state a crash leaves. The dispatcher is crashed between publish and mark by
replacing one method. The clock is then advanced past the lease.

**Exhaustive tables.** Every one of the 121 state pairs is asserted allowed or forbidden against a
list written independently of the implementation. Every restricted endpoint is called as a viewer.

**A static guard.** An AST test fails if any module other than `event_state.py` assigns an event's
status or bulk-updates it. A companion test feeds the guard a violation to prove it detects one.

**Invariants after realistic activity.** One scenario drives events down every branch of the state
machine, then checks properties of the whole database: each event's history starts at `RECEIVED`,
follows only legal edges and ends at its current status; attempt numbers are gapless per cycle; a
delivered event has exactly one successful attempt; the destination received exactly one request
per recorded attempt.

**Scripted model adversaries.** Model behaviour is replayed from a script, including a model that
"obeys" an injected instruction and tries eight state-changing or ambient tools. The guarantees are
asserted on what the system did, not on what a model chose to do.

**Contract fakes for external APIs.** Provider adapters are tested against recorded request and
response shapes, so the tests pin the wire format and run offline.

## What is mocked, and what is not

| Not mocked | Mocked or simulated |
|---|---|
| PostgreSQL, migrations, row locks, unique constraints | Google Pub/Sub (in-memory publisher; a recorded-shape fake for the client) |
| The API and worker HTTP layers (`TestClient`, and real uvicorn in e2e) | Google OIDC verification (an injected verifier) |
| The destination (a real HTTP server on loopback) | Model providers (scripted, or `MockTransport`) |
| The local queue and its pump | DNS, in SSRF tests (an injected resolver) |
| Password hashing, HMAC, sessions | The clock and random source |

## Deliberately not covered, and why

| Gap | Reason | Risk |
|---|---|---|
| Real Pub/Sub, Cloud Run, Scheduler, Secret Manager, Workload Identity | No cloud credentials; nothing is deployed | Medium. Permissions, audiences and ingress settings are the likeliest first-deploy problems. |
| Real Google OIDC token verification | Needs a Google-signed token | Low. The claim checks are tested; the library call is one line. |
| The Pub/Sub emulator | Needs Java or Docker, neither present | Low. The envelope contract is tested and the local queue reproduces the semantics. |
| Running the container images; `docker compose` | Docker not installed on the development machine. CI builds both images but does not start them. | Medium. |
| A live model (Ollama or hosted) | Ollama not installed; no API key used | Medium for report *quality*; low for safety, which does not depend on the model. |
| TLS to a real destination, in CI | Needs the network | Checked by hand once against a public host (valid certificate accepted, mismatched certificate refused). |
| Cloudflare Pages Function proxy | Needs a Pages project | Medium. Written from the documented API, unexecuted. |
| Load and performance | Out of scope; no performance claim is made | — |
| Browsers other than Chromium; assistive-technology audit | Not done | Low to medium. |

## Running

```bash
make test              # all Python tests (starts the local database if needed)
make test-unit         # no database
make test-integration  # database-backed
make test-e2e          # real servers, real time
make test-web          # Vitest
make test-web-e2e      # Playwright; starts its own stack on spare ports with a throwaway database
make coverage          # with the 80% floor on relay_core
make evals             # eval scorecard, deterministic investigator
make secret-scan       # the CI secret scan, locally (needs gitleaks or Docker)
make check             # everything CI runs on a pull request, except browser e2e and security scans
                       # (run `make secret-scan` too before pushing: it is the gate `make check` omits)
```

Database-backed tests refuse to run against a database whose name does not contain `test`, because
they truncate tables. Set `RELAY_TEST_DATABASE_URL` to use a different server.

## CI gates

A pull request must pass, in `.github/workflows/ci.yml`: format check, lint, strict type check,
migrations up → drift check → down, all Python tests, the coverage floor, the eval fixtures; web
lint, type check, unit tests and build; browser e2e; dependency audits, a secret scan and SBOM
generation; Terraform format and validation; and a build of both container images. The deploy
workflow reuses the same file, so nothing reaches staging without passing it.

Live-model evaluation (`python -m evals.run_evals --live`) is manual and never part of CI: it costs
money with a hosted provider and its result is not deterministic.
