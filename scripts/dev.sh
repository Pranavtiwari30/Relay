#!/usr/bin/env bash
# Run the API, worker and web UI together for local development.
# Ctrl-C stops all three. Requires `make setup` to have been run once.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "No .env found. Run 'make setup' first." >&2; exit 1; }
scripts/local-postgres.sh start >/dev/null

pids=()
cleanup() {
  trap - INT TERM EXIT
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

uv run --env-file .env uvicorn relay_api.main:app_factory --factory --host 127.0.0.1 --port 8000 &
pids+=($!)
uv run --env-file .env uvicorn relay_worker.main:app_factory --factory --host 127.0.0.1 --port 8001 &
pids+=($!)
if [ -d apps/web/node_modules ]; then
  (cd apps/web && npm run dev) &
  pids+=($!)
else
  echo "apps/web/node_modules missing: run 'make install' to get the web UI." >&2
fi

echo "API http://127.0.0.1:8000 (docs at /docs) | worker http://127.0.0.1:8001 | UI http://localhost:5173"
wait
