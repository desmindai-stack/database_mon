"""Ölçüm aracı: gerçek sunuculara sorgu yükü üretir (Faz 31 Commit 10a).

Bekleme örnekleyicisinin ölçümü ANLAMLI olsun diye hedef sunucularda gerçekten aktif oturum olmalı; boş
sunucuda örnekleyici "hiçbir şey görmedi" der ve meta veritabanına neredeyse hiç satır yazmaz — yazma hacmi
ölçümü bu yüzden yanıltıcı olurdu. Yük ayrı bir süreçte: ölçülen worker'ın olay döngüsünü etkilemesin.

Yük şekli (parametreyle değişir):
- `--shapes` FARKLI sorgu yapısı. PostgreSQL `query_id` sabitleri normalleştirir; yapısı farklı sorgu farklı
  kimlik demek. Her şekil farklı sayıda AND koşulu içeriyor → farklı kimlik.
- Her sunucuda `--workers` eşzamanlı oturum; sorgu başına kısa uyku (pg_sleep / WAITFOR) → bekleme olayı.
- Her ~8 sn'de bir sunucu başına bir KİLİT çatışması (bir oturum satırı tutuyor, ikincisi bekliyor) → bekleme
  kategorisi çeşitliliği ve bloklama olayı.

Yalnızca gerçek sunuculara `--targets` ile verilen bağlantılar kullanılır; süre dolunca temiz çıkar.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def pg_statement(shape: int) -> str:
    conds = " AND ".join(f"g % {7 + i * 2} <> {i % 5}" for i in range(shape + 1))
    return f"SELECT count(*), pg_sleep(0.05) FROM generate_series(1, 150000) g WHERE {conds}"


async def pg_worker(dsn: str, shapes: int, stop: float) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        while time.monotonic() < stop:
            await conn.fetch(pg_statement(random.randrange(shapes)))
            await asyncio.sleep(random.uniform(0.0, 0.15))
    finally:
        await conn.close()


async def pg_locker(dsn: str, stop: float) -> None:
    import asyncpg

    holder = await asyncpg.connect(dsn)
    waiter = await asyncpg.connect(dsn)
    await holder.execute("CREATE TABLE IF NOT EXISTS sampler_probe_lock (id int PRIMARY KEY, v int)")
    await holder.execute("INSERT INTO sampler_probe_lock VALUES (1, 0) ON CONFLICT DO NOTHING")
    try:
        while time.monotonic() < stop:
            tx = holder.transaction()
            await tx.start()
            await holder.execute("UPDATE sampler_probe_lock SET v = v + 1 WHERE id = 1")
            blocked = asyncio.create_task(waiter.execute("UPDATE sampler_probe_lock SET v = v + 1 WHERE id = 1"))
            await asyncio.sleep(3.0)
            await tx.commit()
            await blocked
            await asyncio.sleep(5.0)
    finally:
        await holder.close()
        await waiter.close()


def mssql_worker(target, shapes: int, stop: float) -> None:
    import pyodbc

    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    conn = pyodbc.connect(build_odbc_connection_string(target), autocommit=True)
    cur = conn.cursor()
    try:
        while time.monotonic() < stop:
            k = random.randrange(shapes) + 1
            conds = " AND ".join(f"a.object_id % {7 + i * 2} <> {i % 5}" for i in range(k))
            cur.execute(f"WAITFOR DELAY '00:00:00.050'; SELECT COUNT(*) FROM sys.all_objects a "
                        f"CROSS JOIN (SELECT TOP 20 object_id FROM sys.all_objects) b WHERE {conds}").fetchall()
            time.sleep(random.uniform(0.0, 0.15))
    finally:
        conn.close()


def mssql_locker(target, database: str, stop: float) -> None:
    import pyodbc

    from app.collectors.sqlserver_mongodb import build_odbc_connection_string
    from tests.live_mssql import SA_PASSWORD, standalone_target

    del target
    admin = build_odbc_connection_string(standalone_target("sa", SA_PASSWORD, database))
    holder = pyodbc.connect(admin, autocommit=False)
    waiter = pyodbc.connect(admin, autocommit=True)
    holder.cursor().execute("IF OBJECT_ID('dbo.sampler_probe_lock') IS NULL CREATE TABLE dbo.sampler_probe_lock "
                            "(id int PRIMARY KEY, v int); IF NOT EXISTS (SELECT 1 FROM dbo.sampler_probe_lock) "
                            "INSERT dbo.sampler_probe_lock VALUES (1, 0)")
    holder.commit()
    import threading
    try:
        while time.monotonic() < stop:
            holder.cursor().execute("UPDATE dbo.sampler_probe_lock SET v = v + 1 WHERE id = 1")
            thread = threading.Thread(
                target=lambda: waiter.cursor().execute("UPDATE dbo.sampler_probe_lock SET v = v + 1 WHERE id = 1"))
            thread.start()
            time.sleep(3.0)
            holder.commit()
            thread.join()
            time.sleep(5.0)
    finally:
        holder.close()
        waiter.close()


async def main(args) -> None:
    from tests.live_mssql import MONITOR_LOGIN, MONITOR_PASSWORD, SA_PASSWORD, standalone_target

    stop = time.monotonic() + args.seconds
    tasks = []
    for dsn in filter(None, args.pg.split(",")):
        tasks += [asyncio.create_task(pg_worker(dsn, args.shapes, stop)) for _ in range(args.workers)]
        tasks.append(asyncio.create_task(pg_locker(dsn, stop)))
    loop = asyncio.get_running_loop()
    if args.mssql:
        target = standalone_target("sa", SA_PASSWORD, "dbace_it_app")
        for _ in range(args.workers):
            tasks.append(loop.run_in_executor(None, mssql_worker, target, args.shapes, stop))
        tasks.append(loop.run_in_executor(None, mssql_locker, target, "dbace_it_app", stop))
    del MONITOR_LOGIN, MONITOR_PASSWORD
    print(f"yük başladı: {len(tasks)} görev, {args.seconds} sn", flush=True)
    await asyncio.gather(*tasks, return_exceptions=True)
    print("yük bitti", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pg", default="", help="virgüllü asyncpg DSN listesi (uygulama veritabanı, sa/postgres)")
    parser.add_argument("--mssql", action="store_true")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--shapes", type=int, default=30)
    parser.add_argument("--workers", type=int, default=3)
    asyncio.run(main(parser.parse_args()))
