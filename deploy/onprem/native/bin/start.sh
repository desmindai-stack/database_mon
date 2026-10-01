#!/usr/bin/env bash
# ExecStart for dbace.service - the native counterpart of the Docker package's entrypoint.sh
# (docs/ONPREM_NATIVE.md section 4): applies migrations on EVERY start (not just install), then
# starts the API + collector (RUN_MODE=all) or the worker. install.sh copies this file to
# releases/<version>/bin/start.sh; it locates its own release directory from $0.
set -euo pipefail

RELEASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RELEASE_DIR"
PY="$RELEASE_DIR/venv/bin/python"

if [ -n "${PG_BINDIR:-}" ] && [ -x "$PG_BINDIR/pg_isready" ]; then
  echo "Waiting for PostgreSQL..."
  for _ in $(seq 1 30); do
    "$PG_BINDIR/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break
    sleep 1
  done
fi

"$PY" -m app.migrations_runner "$RELEASE_DIR/migrations"

if [ "${RUN_MODE:-all}" = "worker" ]; then
  exec "$PY" -m app.worker
fi

exec "$RELEASE_DIR/venv/bin/uvicorn" app.main:app --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8000}"
