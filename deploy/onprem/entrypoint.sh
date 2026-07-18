#!/bin/sh
set -e

if [ -n "$DATABASE_URL" ]; then
  echo "Waiting for PostgreSQL..."
  python - <<'PY'
import os, sys, time
import asyncio
async def wait():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return
    try:
        import asyncpg
    except ImportError:
        return
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    for i in range(60):
        try:
            conn = await asyncpg.connect(dsn, timeout=3)
            await conn.close()
            print("PostgreSQL is ready.")
            return
        except Exception:
            time.sleep(2)
    print("PostgreSQL wait timeout.", file=sys.stderr)
    sys.exit(1)
asyncio.run(wait())
PY
fi

PORT="${PORT:-8000}"

if [ "$RUN_MODE" = "worker" ]; then
  exec python -m app.worker
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
