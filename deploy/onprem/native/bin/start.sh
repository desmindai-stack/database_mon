#!/usr/bin/env bash
# dbace.service'in ExecStart'ı — Docker paketindeki entrypoint.sh'in native karşılığı
# (docs/ONPREM_NATIVE.md §4): HER başlangıçta (yalnızca kurulumda değil) migration'ları uygular,
# sonra API + toplayıcıyı (RUN_MODE=all) ya da worker'ı başlatır. install.sh bu dosyayı
# releases/<sürüm>/bin/start.sh olarak kopyalar; kendi release dizinini $0'dan bulur.
set -euo pipefail

RELEASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RELEASE_DIR"
PY="$RELEASE_DIR/venv/bin/python"

if [ -n "${PG_BINDIR:-}" ] && [ -x "$PG_BINDIR/pg_isready" ]; then
  echo "PostgreSQL bekleniyor..."
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
