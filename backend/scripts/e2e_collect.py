"""E2E için GERÇEK veri: canlı PostgreSQL'de iş yükü + gerçek toplama turları (Faz 31 Commit 7).

    DATABASE_URL=<e2e veritabanı> python scripts/e2e_collect.py <instance_id> <yönetici DSN>

E2E backend'i RUN_MODE=api ile açılıyor (toplayıcı kapalı). Rozet = görünen kalem testinin sahte değil
ölçülmüş veriye bakması için bu betik gerçek toplayıcıyı (`collection.collect_instance`) aynı e2e
veritabanına karşı çalıştırıyor. İş yükü: yavaş uygulama sorguları (kısıtlı `dbace_it_app` rolü), hızlı
sorgu, yavaş katalog sorgusu. Roller ve test verisi: tests/live_pg.py (idempotent).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Instance  # noqa: E402
from app.services import collection as collection_module  # noqa: E402
from tests.live_pg import ROLE_PASSWORD, prepare_live_database, target_for  # noqa: E402


async def _workload(dsn: str, tag: str) -> None:
    t = target_for(dsn, "app")
    app_conn = await asyncpg.connect(host=t.host, port=t.port, database=t.database, user=t.username,
                                     password=ROLE_PASSWORD, statement_cache_size=0)
    try:
        for i in range(3):
            await app_conn.fetchval(
                f"SELECT count(*) AS {tag}_{i} FROM orders WHERE status = $1 AND (SELECT pg_sleep(0.06)) IS NOT NULL", "paid")
        for _ in range(5):
            await app_conn.fetchval(f"SELECT count(*) AS {tag}_fast FROM customers WHERE segment = $1", "gold")
    finally:
        await app_conn.close()
    admin = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_class WHERE (SELECT pg_sleep(0.07)) IS NOT NULL LIMIT 1")
    finally:
        await admin.close()


async def _collect(instance_id: int) -> None:
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()
    await asyncio.sleep(1.1)


async def main(instance_id: int, dsn: str) -> None:
    await init_db()
    admin = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await prepare_live_database(admin)
        await admin.execute("SELECT pg_stat_statements_reset()")
    finally:
        await admin.close()
    tag = f"e2e{uuid.uuid4().hex[:6]}"
    await _collect(instance_id)
    for _ in range(2):
        await _workload(dsn, tag)
        await _collect(instance_id)
    print(f"e2e verisi hazır: instance {instance_id}, etiket {tag}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
