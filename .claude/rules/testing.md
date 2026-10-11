---
paths:
  - "tests/**"
  - "evals/**"
  - "apps/web/src/**/*.test.*"
  - "apps/web/e2e/**"
---

# Testing rules

Read `docs/quality/testing-strategy.md` for the layers and what each proves.

- **Report truthfully.** Run the tests before saying they pass. If something could not be run, say
  so and why. Never mark a test as passing by weakening it.
- **A test must be able to fail.** No `or True`, no assertion on a value the test itself set, no
  catching the exception you are testing for. When you write a guard, feed it a violation.
- **Deterministic.** No real sleeps in unit or integration tests: use `FakeClock` and advance it.
  Seed or inject randomness. Only `tests/e2e` and Playwright use real time.
- **Real database for database behaviour.** Locks, unique constraints and `SKIP LOCKED` are tested
  on PostgreSQL, built by running the migrations. Do not substitute SQLite or mocks.
- **Drive through the public interface** where practical: HTTP for the API and worker, the stub
  destination for delivery. Reaching into a private method is acceptable only to simulate a crash
  between steps.
- **Tests are independent.** Each starts from truncated tables. Do not rely on ordering.
- Use the `Harness` in `tests/support.py`; extend it rather than duplicating setup.
- New behaviour needs a test that would have failed before the change. A bug fix starts with a
  failing test.
- Security-relevant changes (auth, RBAC, SSRF, signatures, redaction, assistant tools) need a test
  in `tests/security`.
- **Fixtures must not look like real credentials.** The secret scan covers `tests/` too. Never use
  a vendor's key format (`sk_live_…`, `whsec_…`, `AKIA…`, `ghp_…`, a JWT) even with fake content;
  use plainly synthetic values such as `test-webhook-signing-secret`. Reuse the constants in
  `tests/support.py` rather than repeating literals. Run `make secret-scan` before pushing.
- Normal CI never calls a live model. Use `ScriptedProvider` or `httpx.MockTransport`.
- Coverage floor is 80% on `relay_core`. Do not add tests whose only purpose is the number.

## Commands

```bash
uv run pytest tests/unit                       # fast, no database
uv run pytest tests/integration -k replay      # by keyword
uv run pytest tests/path/test_file.py::test_x  # one test
make coverage                                  # with the floor
cd apps/web && npm test                        # Vitest
make test-web-e2e                              # Playwright
```
