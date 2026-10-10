#!/usr/bin/env bash
# Start an isolated Relay stack for browser end-to-end tests.
#
# Uses its own database (relay_e2e, recreated on every run) and spare ports, so
# it never touches development data or collides with `make dev`.
# Every credential below is a throwaway test fixture for that disposable database.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="${RELAY_E2E_API_PORT:-8100}"
WORKER_PORT="${RELAY_E2E_WORKER_PORT:-8101}"
WEB_PORT="${RELAY_E2E_WEB_PORT:-5273}"
PGPORT="${RELAY_PGPORT:-54329}"

export RELAY_ENV=local
export RELAY_LOG_LEVEL=WARNING
export RELAY_LOG_FORMAT=console
export RELAY_DATABASE_URL="${RELAY_E2E_DATABASE_URL:-postgresql+psycopg://relay@127.0.0.1:${PGPORT}/relay_e2e}"
export RELAY_CORS_ALLOWED_ORIGINS="http://127.0.0.1:${WEB_PORT}"
export RELAY_QUEUE_BACKEND=local
export RELAY_BACKGROUND_LOOPS=true
export RELAY_DISPATCH_INTERVAL_SECONDS=0.2
export RELAY_WORKER_BASE_URL="http://127.0.0.1:${WORKER_PORT}"
export RELAY_DEMO_DESTINATION_BASE_URL="http://127.0.0.1:${API_PORT}"
export RELAY_INTERNAL_TOKEN="e2e-internal-token-not-a-secret"
export RELAY_SECRET_DEMO_WEBHOOK="e2e-webhook-secret-not-a-secret"
export RELAY_SECRET_DEMO_DESTINATION_TOKEN="e2e-destination-token-not-a-secret"
export RELAY_LOGIN_RATE_LIMIT_PER_15_MINUTES=1000
export RELAY_SEED_ADMIN_EMAIL="admin@e2e.test"
export RELAY_SEED_ADMIN_PASSWORD="e2e-admin-password-1"
export RELAY_SEED_OPERATOR_EMAIL="operator@e2e.test"
export RELAY_SEED_OPERATOR_PASSWORD="e2e-operator-password-1"
export RELAY_SEED_VIEWER_EMAIL="viewer@e2e.test"
export RELAY_SEED_VIEWER_PASSWORD="e2e-viewer-password-1"
export RELAY_API_PROXY_TARGET="http://127.0.0.1:${API_PORT}"
export RELAY_WEB_PORT="${WEB_PORT}"

if [ -z "${RELAY_E2E_DATABASE_URL:-}" ]; then
  scripts/local-postgres.sh start >/dev/null
  dropdb --if-exists -h 127.0.0.1 -p "$PGPORT" -U relay relay_e2e
  createdb -h 127.0.0.1 -p "$PGPORT" -U relay relay_e2e
fi
uv run relay-migrate upgrade >/dev/null 2>&1
uv run python scripts/seed.py >/dev/null

pids=()
cleanup() {
  trap - INT TERM EXIT
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

uv run uvicorn relay_api.main:app_factory --factory --host 127.0.0.1 --port "$API_PORT" --log-level warning &
pids+=($!)
uv run uvicorn relay_worker.main:app_factory --factory --host 127.0.0.1 --port "$WORKER_PORT" --log-level warning &
pids+=($!)
(cd apps/web && exec npx vite --host 127.0.0.1 --port "$WEB_PORT" --strictPort) &
pids+=($!)
wait
