#!/usr/bin/env bash
# Post-deploy smoke test. Read-only: it creates no data and needs no credentials.
#   scripts/smoke-test.sh https://relay-api-staging-xxxx.a.run.app
set -euo pipefail
BASE="${1:?usage: smoke-test.sh <api-base-url>}"
BASE="${BASE%/}"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

check() { # check <description> <expected-status> <curl args...>
  local description="$1" expected="$2"; shift 2
  local actual
  actual=$(curl -sS -o /tmp/relay-smoke-body -w '%{http_code}' --max-time 30 "$@") || fail "$description: request error"
  [ "$actual" = "$expected" ] || { cat /tmp/relay-smoke-body >&2 || true; fail "$description: expected HTTP $expected, got $actual"; }
  echo "ok   $description (HTTP $actual)"
}

# Cold starts are expected with min-instances=0: allow the first request to be slow.
check "liveness" 200 "$BASE/healthz"
check "readiness (database reachable, schema current)" 200 "$BASE/readyz"
check "OpenAPI document is served" 200 "$BASE/openapi.json"
check "control plane rejects anonymous callers" 401 "$BASE/api/v1/events"
check "unknown webhook endpoint is refused" 404 -X POST -H 'Content-Type: application/json' -d '{}' \
  "$BASE/webhooks/00000000-0000-0000-0000-000000000000"
check "oversized webhook body is refused" 413 -X POST -H 'Content-Type: application/json' \
  --data-binary "$(head -c 400000 /dev/zero | tr '\0' 'x')" "$BASE/webhooks/00000000-0000-0000-0000-000000000000"
grep -q '"correlation_id"' /tmp/relay-smoke-body || fail "error envelope has no correlation_id"
echo "Smoke test passed for $BASE"
