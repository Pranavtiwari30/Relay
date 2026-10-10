#!/usr/bin/env bash
# Project-local PostgreSQL cluster for development and tests.
#
# Runs an isolated cluster in .local/pgdata on port 54329 so Relay never touches
# databases in a system-wide PostgreSQL install. Needs only the PostgreSQL
# binaries (initdb, pg_ctl, createdb) on PATH — no Docker.
#
# Usage: scripts/local-postgres.sh {start|stop|status|reset}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="${RELAY_PGDATA:-$ROOT/.local/pgdata}"
PGPORT="${RELAY_PGPORT:-54329}"
PGUSER_NAME="relay"
LOGFILE="$ROOT/.local/postgres.log"

for bin in initdb pg_ctl createdb pg_isready; do
  command -v "$bin" >/dev/null 2>&1 || {
    echo "error: '$bin' not found on PATH. Install PostgreSQL 15+ (e.g. 'brew install postgresql@16')" >&2
    echo "       or use 'docker compose up -d db' instead." >&2
    exit 1
  }
done

is_running() { pg_ctl -D "$PGDATA" status >/dev/null 2>&1; }

start() {
  mkdir -p "$ROOT/.local"
  if [ ! -f "$PGDATA/PG_VERSION" ]; then
    echo "Initialising cluster in $PGDATA"
    # Trust auth is acceptable here only because the server listens on loopback
    # and a private socket directory; it holds development data exclusively.
    initdb -D "$PGDATA" -U "$PGUSER_NAME" --auth=trust --encoding=UTF8 --locale=C >/dev/null
  fi
  if is_running; then
    echo "PostgreSQL already running on port $PGPORT"
  else
    pg_ctl -D "$PGDATA" -l "$LOGFILE" -w \
      -o "-p $PGPORT -c listen_addresses=127.0.0.1 -c unix_socket_directories='$PGDATA'" start >/dev/null
    echo "PostgreSQL started on 127.0.0.1:$PGPORT"
  fi
  for db in relay relay_test; do
    createdb -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" "$db" 2>/dev/null && echo "Created database $db" || true
  done
}

stop() {
  if is_running; then
    pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null
    echo "PostgreSQL stopped"
  else
    echo "PostgreSQL is not running"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status)
    if is_running; then
      pg_isready -h 127.0.0.1 -p "$PGPORT"
    else
      echo "PostgreSQL is not running"
      exit 3
    fi
    ;;
  reset)
    stop
    rm -rf "$PGDATA"
    echo "Removed $PGDATA"
    start
    ;;
  *)
    echo "usage: $0 {start|stop|status|reset}" >&2
    exit 2
    ;;
esac
