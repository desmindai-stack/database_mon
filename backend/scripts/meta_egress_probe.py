"""Meta veritabanı egress ölçümü (Faz 31 Commit 9, madde 0).

Canlıda Supabase egress kotası 17 kat aşıldı: uygulama meta veritabanından satırları çekip Python'da
işliyordu. Bu betik aynı ölçümü YEREL bir PostgreSQL meta veritabanında tekrarlanabilir kılıyor:

1. `--seed`: ayrı bir PostgreSQL konteynerinde (varsayılan `dbace-meta-egress`, port 55450) meta veritabanını
   migration çalıştırıcısıyla kurar, canlı ölçeğinde geçmiş üretir (slow_query_samples ≈ 390 bin satır,
   30 gün; metric_samples 15 sn aralıkla 30 gün; schema_object_daily_samples 90 gün) ve sihirbazla GERÇEK
   izlenen hedefler ekler (scripts/live_pg.py'nin 15/16/17 konteynerleri, paketin salt-okunur rolü).
2. Her KOD YOLUNU ayrı ayrı koşar ve arasında `pg_stat_statements_reset()` yapar:
   - zamanlayıcının her işi (app/collectors/scheduler.py) bir tur,
   - OpenAPI'deki HER GET ucu (elle liste yok; yol parametresi doldurulamayan uç "atlandı" diye raporlanır).
3. Her yol için: meta veritabanında dönen satır (pg_stat_statements.rows), çağrı sayısı ve konteynerin ağ
   arayüzünden GİDEN bayt (egress'in kendisi — konteynerde replika yok, trafik yalnızca uygulamaya).

    python scripts/meta_egress_probe.py --seed --out before.json
    python scripts/meta_egress_probe.py --out after.json          # aynı veriyle yeniden ölç
    python scripts/meta_egress_probe.py --compare before.json after.json

Meta veritabanı dbace'in KENDİ veritabanı; ölçüm için süper kullanıcı kullanılıyor (izlenen hedefler değil).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

CONTAINER = "dbace-meta-egress"
PORT = 55450
DATABASE = "dbace_meta"
ADMIN_DSN = f"postgresql://postgres:dbace@127.0.0.1:{PORT}"
TARGET_PORTS = {15: 55433, 16: 55434, 17: 55432}


def tx_bytes() -> int:
    out = subprocess.run(["docker", "exec", CONTAINER, "cat", "/proc/net/dev"], capture_output=True, text=True,
                         check=True).stdout
    line = next(line for line in out.splitlines() if line.strip().startswith("eth0:"))
    return int(line.split(":", 1)[1].split()[8])


def ensure_container() -> None:
    running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True)
    if running.stdout.strip() == "true":
        return
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", CONTAINER, "-p", f"{PORT}:5432", "-e", "POSTGRES_PASSWORD=dbace",
                    "postgres:17", "postgres", "-c", "shared_preload_libraries=pg_stat_statements",
                    "-c", "pg_stat_statements.max=10000"], check=True, capture_output=True)
    time.sleep(8)


# --- Veri üretimi --------------------------------------------------------------------------------

_SEED_SQL = """
-- 40 farklı, ~800 baytlık sorgu metni (canlıda satır başına ~840 bayt; metnin çoğu tekrar ediyor).
CREATE TEMP TABLE seed_queries AS
SELECT q, 'SELECT o.id, o.customer_id, o.status, o.total, o.created_at, c.name, c.segment, c.region /* rapor ' || q
          || ' */ FROM orders_' || q || ' o JOIN customers c ON c.id = o.customer_id WHERE o.status = $1 AND o.created_at >= $2 '
          || repeat('AND o.flag_' || q || ' IS NOT NULL ', 18) || 'ORDER BY o.created_at DESC LIMIT $3' AS text
FROM generate_series(1, 40) q;

INSERT INTO slow_query_samples (instance_id, collected_at, queryid, query, from_monitoring_role, toplevel, calls,
                                total_time_ms, mean_time_ms, rows, shared_blks_hit, shared_blks_read, stddev_time_ms,
                                min_time_ms, max_time_ms)
SELECT i.id, now() - make_interval(mins => t * 5), (1000000 + ((t + k) % 40))::text, sq.text, false, true,
       100000 - t * 10 + k, (100000 - t * 10 + k) * (5 + ((t + k) % 40) * 3.1), 5 + ((t + k) % 40) * 3.1,
       (100000 - t * 10) * 3, 500000 - t * 40, 2000 - (t % 1000), 1.5, 0.2, 90.0
FROM instances i
CROSS JOIN generate_series(0, 8639) t
CROSS JOIN generate_series(1, 15) k
JOIN seed_queries sq ON sq.q = ((t + k) % 40) + 1
WHERE i.id = ANY($1::int[]);

INSERT INTO metric_samples (instance_id, collected_at, active_connections, max_connections, transactions_per_sec,
                            cache_hit_ratio, database_size_bytes, deadlocks, temp_bytes, metrics_json)
SELECT i.id, now() - make_interval(secs => t * 15), 10 + t % 7, 100, 50 + t % 13, 99.1, 1e9 + t, 3, 0,
       (SELECT jsonb_object_agg('metric_' || m, (t + m) % 997) FROM generate_series(1, 40) m)
FROM instances i CROSS JOIN generate_series(0, 172799) t
WHERE i.id = ANY($1::int[]);

INSERT INTO schema_object_daily_samples (instance_id, day, object_kind, schema_name, object_name, size_bytes, extra)
SELECT i.id, current_date - d, CASE WHEN o % 3 = 0 THEN 'index' ELSE 'table' END, 'public', 'obj_' || o,
       1e6 * o + d * 1000, '{"rows": 1000}'::jsonb
FROM instances i CROSS JOIN generate_series(0, 89) d CROSS JOIN generate_series(1, 80) o
WHERE i.id = ANY($1::int[]);
ANALYZE;
"""


async def seed() -> list[int]:
    import asyncpg

    from app.migrations_runner import apply_migrations
    from tests.live_pg import ROLE_PASSWORD, prepare_restricted_database, restricted_target

    admin = await asyncpg.connect(f"{ADMIN_DSN}/postgres")
    await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {DATABASE}")
    await admin.close()
    meta = await asyncpg.connect(f"{ADMIN_DSN}/{DATABASE}")
    await meta.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    applied = await apply_migrations(meta, BACKEND.parent / "supabase" / "migrations")
    await meta.close()
    print(f"migration: {len(applied)}")

    from tests.auth_helper import authed_client

    ids = []
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": "egress-ölçüm"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        for version, port in TARGET_PORTS.items():
            dsn = f"postgresql://postgres:dbace@127.0.0.1:{port}/dbace"
            await prepare_restricted_database(dsn)
            t = restricted_target(dsn)
            group = (await client.post("/api/wizard/database-groups", json={
                "application_id": application["id"], "group_name": f"pg{version}", "engine": "postgresql",
                "topology": "standalone",
                "nodes": [{"server_name": f"pg{version}", "host": t.host, "port": t.port, "database": t.database,
                           "db_username": t.username, "db_password": ROLE_PASSWORD}],
            })).json()
            ids.append((await client.get(f"/api/groups/{group['id']}/nodes")).json()[0]["instance_id"])
    meta = await asyncpg.connect(f"{ADMIN_DSN}/{DATABASE}")
    started = time.monotonic()
    await meta.execute("SET work_mem = '256MB'")
    for statement in _SEED_SQL.split(";\n"):
        body = "\n".join(line for line in statement.splitlines() if not line.strip().startswith("--"))
        if "ANY($1" in body:
            await meta.execute(body, ids)
        elif body.strip():
            await meta.execute(body)
    counts = {t: await meta.fetchval(f"SELECT count(*) FROM {t}") for t in
              ("slow_query_samples", "metric_samples", "schema_object_daily_samples")}
    size = await meta.fetchval("SELECT pg_size_pretty(pg_total_relation_size('slow_query_samples'))")
    await meta.close()
    print(f"veri: {counts}, slow_query_samples {size}, {time.monotonic() - started:.0f} sn")
    return ids


# --- Ölçüm ---------------------------------------------------------------------------------------


async def _reset(admin) -> None:
    await admin.execute("SELECT pg_stat_statements_reset()")


async def _statements(admin) -> list[dict]:
    rows = await admin.fetch(
        """
        SELECT query, calls, rows FROM pg_stat_statements
        WHERE dbid = (SELECT oid FROM pg_database WHERE datname = $1) AND query NOT ILIKE '%pg_stat_statements%'
        ORDER BY rows DESC
        """,
        DATABASE,
    )
    return [{"query": re.sub(r"\s+", " ", r["query"])[:400], "calls": r["calls"], "rows": r["rows"]} for r in rows]


def _jobs():
    from app.collectors import scheduler
    from app.services.query_text_privacy import run_stored_text_cleanup
    from app.database import SessionLocal

    async def cleanup_forced():
        async with SessionLocal() as session:
            await run_stored_text_cleanup(session, force=True)

    # Zamanlayıcının kaydettiği işler; sıklık app/collectors/scheduler.py::start_scheduler'dan.
    return [
        ("iş: collect_all_instances (15 sn)", scheduler.collect_all_instances),
        ("iş: refresh_dashboard_snapshots (60 sn)", scheduler.refresh_dashboard_snapshots),
        ("iş: evaluate_custom_rules_tick", scheduler.evaluate_custom_rules_tick),
        ("iş: wait_sampling_tick (1 sn)", scheduler.wait_sampling_tick),
        ("iş: plan_capture_tick (300 sn)", scheduler.plan_capture_tick),
        ("iş: index_advice_watch_tick (300 sn)", scheduler.index_advice_watch_tick),
        ("iş: backup_tick (900 sn)", scheduler.backup_tick),
        ("iş: prediction_accuracy_tick (saatlik)", scheduler.prediction_accuracy_tick),
        ("iş: daily_rollup_tick (03:30)", scheduler.daily_rollup_tick),
        ("iş: daily_health_report_tick (günlük)", scheduler.daily_health_report_tick),
        ("iş: stored_text_cleanup (tek sefer, force)", cleanup_forced),
    ]


async def _route_params(client, instance_id: int) -> dict:
    group_id = None
    for group in (await client.get("/api/groups")).json():
        nodes = (await client.get(f"/api/groups/{group['id']}/nodes")).json()
        if any(n.get("instance_id") == instance_id for n in nodes):
            group_id = group["id"]
            break
    report = (await client.post("/api/reports/run", json={"scope_type": "instance", "scope_id": instance_id,
                                                          "period_days": 1})).json()
    for _ in range(240):
        if (await client.get(f"/api/reports/{report['id']}")).json().get("status") not in ("pending", "running"):
            break
        await asyncio.sleep(0.5)
    customers = (await client.get("/api/customers")).json()
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Node, SlowQuerySample

    async with SessionLocal() as session:
        node = (await session.execute(select(Node).where(Node.instance_id == instance_id))).scalar_one()
        sample = (await session.execute(select(SlowQuerySample.id, SlowQuerySample.queryid)
                                        .where(SlowQuerySample.instance_id == instance_id).limit(1))).one()
    return {"instance_id": instance_id, "group_id": group_id, "report_id": report["id"], "id": instance_id,
            "customer_id": customers[0]["id"], "node_id": node.id, "server_id": node.server_id,
            "queryid": sample.queryid, "sample_id": sample.id, "scope": "instance", "scope_id": instance_id}


async def measure(out: Path) -> dict:
    import asyncpg

    from app.main import app
    from tests.auth_helper import authed_client

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Instance

    async with SessionLocal() as session:
        instance_ids = [i for (i,) in (await session.execute(select(Instance.id).order_by(Instance.id))).all()]
    instance_id = instance_ids[0]
    admin = await asyncpg.connect(f"{ADMIN_DSN}/{DATABASE}")
    results: dict[str, dict] = {}

    async def run(label: str, kind: str, coro_factory) -> None:
        await _reset(admin)
        before = tx_bytes()
        started = time.monotonic()
        error = None
        try:
            await coro_factory()
        except Exception as exc:  # noqa: BLE001 — ölçüm aracı; hata da sonuçtur
            error = f"{type(exc).__name__}: {exc}"[:300]
        elapsed = time.monotonic() - started
        sent = tx_bytes() - before
        statements = await _statements(admin)
        results[label] = {
            "kind": kind, "rows": sum(s["rows"] for s in statements), "calls": sum(s["calls"] for s in statements),
            "tx_bytes": sent, "seconds": round(elapsed, 2), "error": error, "statements": statements[:6],
        }
        print(f"{label[:70]:70} rows={results[label]['rows']:>9} calls={results[label]['calls']:>6} "
              f"tx={sent / 1024:>9.0f} KB{'  HATA ' + error[:60] if error else ''}", flush=True)

    for label, job in _jobs():
        await run(label, "job", job)

    async with await authed_client() as client:
        params = await _route_params(client, instance_id)
        skipped = []
        for path, ops in sorted(app.openapi()["paths"].items()):
            get = ops.get("get")
            if not get:
                continue
            names = re.findall(r"\{([a-z_]+)\}", path)
            required = [p["name"] for p in get.get("parameters", []) if p.get("in") == "query" and p.get("required")]
            if set(names + required) - set(params):
                skipped.append(path)
                continue
            url = path.format(**{n: params[n] for n in names})
            query = {n: params[n] for n in required}

            async def call(url=url, query=query):
                response = await client.get(url, params=query)
                if response.status_code >= 500:
                    raise RuntimeError(f"{response.status_code} {response.text[:200]}")

            await run(f"GET {path}", "route", call)
    await admin.close()
    payload = {"database": DATABASE, "instances": instance_ids, "skipped_routes": skipped, "results": results}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\natlanan uç (yol parametresi doldurulamadı): {len(skipped)} → {skipped}")
    return payload


def compare(before_path: Path, after_path: Path) -> None:
    before = json.loads(before_path.read_text(encoding="utf-8"))["results"]
    after = json.loads(after_path.read_text(encoding="utf-8"))["results"]
    print("| Yol | Satır (önce) | Satır (sonra) | Egress KB (önce) | Egress KB (sonra) | Oran |")
    print("|---|---:|---:|---:|---:|---:|")
    total = [0, 0, 0, 0]
    for label in sorted(before, key=lambda k: -before[k]["tx_bytes"]):
        b, a = before[label], after.get(label)
        if not a:
            continue
        total = [total[0] + b["rows"], total[1] + a["rows"], total[2] + b["tx_bytes"], total[3] + a["tx_bytes"]]
        if b["tx_bytes"] < 50_000 and b["rows"] < 1000:
            continue
        ratio = b["tx_bytes"] / a["tx_bytes"] if a["tx_bytes"] else float("inf")
        print(f"| {label} | {b['rows']:,} | {a['rows']:,} | {b['tx_bytes'] / 1024:,.0f} | {a['tx_bytes'] / 1024:,.0f} | {ratio:,.1f}× |")
    print(f"| **TOPLAM** | {total[0]:,} | {total[1]:,} | {total[2] / 1024:,.0f} | {total[3] / 1024:,.0f} | "
          f"{total[2] / max(total[3], 1):,.1f}× |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    ensure_container()
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://postgres:dbace@127.0.0.1:{PORT}/{DATABASE}"
    os.environ.setdefault("WAIT_SAMPLING_ENABLED", "true")

    async def go():
        if args.seed:
            await seed()
        if args.out:
            await measure(args.out)

    asyncio.run(go())


if __name__ == "__main__":
    main()
