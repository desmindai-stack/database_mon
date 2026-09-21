"""Bekleme örnekleyicisi ölçümü: gerçek aralık, bileşen süreleri, ölçeklenme, yazma hacmi (Faz 31 Commit 10a).

ÜRETİMDEKİ İŞİ BİREBİR KOŞTURUR: zamanlayıcıya `app.collectors.scheduler`'ın kayıt fonksiyonuyla işler eklenir
(fonksiyon yoksa — eski sürümde — eski satır içi kayıt). Böylece ölçüm, üretimden ayrışan bir kopya değil.

    python scripts/sampler_probe.py --instances 5 --seconds 150 --label onceki-5
    python scripts/sampler_probe.py --instances 1 --target-rtt-ms 150 --meta-rtt-ms 60 --seconds 150

Ölçülenler (hepsi JSON + markdown):
- gerçek örnekleme aralığı: instance başına ardışık örneklerin GELİŞ zamanı farkı (atlanan turlar dahil).
- APScheduler'ın "maximum number of running instances" ile atladığı tur sayısı (olay dinleyicisinden).
- bileşen süreleri: hedefe bağlanma, örnekleme sorgusu, bloklama sorgusu, meta yazımı (flush), instance listesi.
- meta veritabanına gidiş-dönüş sayısı ve süresi (SQLAlchemy olayları).
- meta tablolarına yazılan satır ve bayt (pg_stat_user_tables / pg_total_relation_size farkı).

DÜRÜSTLÜK: instance sayısı gerçek sunucu sayısından (3 PostgreSQL + 3 replika + 2 SQL Server) fazlaysa aynı
sunucuya BİRDEN ÇOK instance kaydı bağlanır (her biri kendi kalıcı bağlantısıyla). Sunucu sayısı raporda yazılır.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

META_CONTAINER = "dbace-meta-egress"
META_PORT = 55450
META_PROXY_PORT = 55460
DATABASE = "dbace_sampler"
ADMIN_DSN = f"postgresql://postgres:dbace@127.0.0.1:{META_PORT}"
PG_PORTS = [55433, 55434, 55432]  # 15, 16, 17 (birincil)
PG_REPLICA_PORTS = [55443, 55444, 55442]
MSSQL_PORTS = {"standalone": 14333, "ag": 14334}
TARGET_PROXY_BASE = 56000  # hedef başına vekil portu: base + sıra
META_TABLES = ("wait_sample_minutes", "active_session_minutes", "wait_query_signatures", "blocking_episodes")


def ensure_meta() -> None:
    running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", META_CONTAINER], capture_output=True, text=True)
    if running.stdout.strip() != "true":
        subprocess.run(["docker", "start", META_CONTAINER], check=True, capture_output=True)
        time.sleep(6)


async def prepare_meta() -> None:
    import asyncpg

    from app.migrations_runner import apply_migrations

    ensure_meta()
    admin = await asyncpg.connect(f"{ADMIN_DSN}/postgres")
    await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {DATABASE}")
    await admin.close()
    meta = await asyncpg.connect(f"{ADMIN_DSN}/{DATABASE}")
    applied = await apply_migrations(meta, BACKEND.parent / "supabase" / "migrations")
    await meta.close()
    print(f"[meta] {len(applied)} migration uygulandı", flush=True)


def targets_catalog():
    """(ad, motor, ConnectionTarget) — hepsi KISITLI izleme kimlikleriyle (superuser yok)."""
    from tests.live_mssql import APP_DATABASE, MONITOR_LOGIN, MONITOR_PASSWORD, mssql_target, standalone_target
    from tests.live_pg import restricted_target

    items = []
    for label, ports in (("pg-primary", PG_PORTS), ("pg-replica", PG_REPLICA_PORTS)):
        for index, port in enumerate(ports):
            items.append((f"{label}{(15, 16, 17)[index]}", "postgresql",
                          restricted_target(f"postgresql://postgres:dbace@127.0.0.1:{port}/dbace")))
    items.append(("mssql-standalone", "sqlserver", standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, APP_DATABASE)))
    ag = mssql_target("ag", "ro")
    items.append(("mssql-ag", "sqlserver", ag))
    return items


async def register_instances(count: int, target_rtt_ms: float, proxy_map: dict[int, int], only: str = "") -> list[int]:
    from app.database import SessionLocal, init_db
    from app.models import Instance
    from app.services.credentials import encrypt_secret

    await init_db()
    catalog = targets_catalog()
    if only:
        catalog = [item for item in catalog if item[1] == ("postgresql" if only == "pg" else "sqlserver")]
    ids = []
    async with SessionLocal() as session:
        for i in range(count):
            name, engine, target = catalog[i % len(catalog)]
            port = proxy_map.get(target.port, target.port) if target_rtt_ms else target.port
            row = Instance(
                name=f"probe-{i:02d}-{name}", engine=engine, host=target.host, port=port,
                database=target.database, username=target.username, password=encrypt_secret(target.password),
                options=target.options or None, enabled=True,
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
    return ids


class Recorder:
    """Bileşen süreleri ve gerçek örnekleme zamanları."""

    def __init__(self) -> None:
        self.durations: dict[str, list[float]] = defaultdict(list)
        self.completed: dict[int, list[float]] = defaultdict(list)  # instance_id -> geliş zamanları (monotonic)
        self.skipped_by_scheduler = 0
        self.ticks: list[float] = []
        self.meta_statements = 0
        self.meta_seconds = 0.0
        self.meta_commits = 0

    def add(self, key: str, seconds: float) -> None:
        self.durations[key].append(seconds)


def install_wrappers(recorder: Recorder, instance_by_conn: dict) -> None:
    """Üretim koduna dokunmadan bileşen zamanlayıcıları: sınıf/işlev düzeyinde sarmalar."""
    from app.collectors.postgresql import PostgreSQLCollector
    from app.collectors.sqlserver_mongodb import SqlServerCollector
    from app.services import wait_sampling

    def wrap_async(owner, name: str, key: str, on_result=None):
        original = getattr(owner, name, None)
        if original is None:
            return

        async def wrapper(*args, **kwargs):
            started = time.monotonic()
            try:
                return await original(*args, **kwargs)
            finally:
                recorder.add(key, time.monotonic() - started)
                if on_result:
                    on_result(args, kwargs)

        setattr(owner, name, wrapper)

    for engine_name, cls in (("pg", PostgreSQLCollector), ("mssql", SqlServerCollector)):
        wrap_async(cls, "open_sampling_connection", f"hedef bağlanma ({engine_name})")

        def make_done(engine=engine_name):
            def done(args, kwargs):
                collector = args[0]
                recorder.completed[id(collector)].append(time.monotonic())
            return done

        wrap_async(cls, "sample_active_sessions", f"hedef örnekleme sorgusu ({engine_name})", make_done())
        wrap_async(cls, "collect_blocking", f"hedef bloklama sorgusu ({engine_name})")

    wrap_async(wait_sampling, "flush_completed_buckets", "meta: flush_completed_buckets (dakika kapanışı)")
    wrap_async(wait_sampling, "_write_bucket", "meta: _write_bucket (kova başına)")
    wrap_async(wait_sampling, "_check_blocking", "bloklama: hedef sorgu + meta yazımı (instance başına)")
    wrap_async(wait_sampling, "_record_pending_blocking", "meta: bekleyen bloklama fotoğrafları yazımı")
    wrap_async(wait_sampling, "_load_instances", "meta: instance listesi")
    from app.collectors import scheduler as scheduler_module

    # Zamanlayıcı yazım işini `flush_tick`'i kendi modül düzeyindeki adıyla çağırır: orayı sarıyoruz.
    if hasattr(scheduler_module, "flush_tick"):
        wrap_async(scheduler_module, "flush_tick", "yazım işi (bir çalışma, sıfır olmayan)")

    from app.database import engine as meta_engine
    from sqlalchemy import event

    starts: dict = {}

    @event.listens_for(meta_engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        starts[id(cursor)] = time.monotonic()

    @event.listens_for(meta_engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        began = starts.pop(id(cursor), None)
        if began is not None:
            recorder.meta_statements += 1
            recorder.meta_seconds += time.monotonic() - began

    @event.listens_for(meta_engine.sync_engine, "commit")
    def _commit(conn):
        recorder.meta_commits += 1


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def summarize_durations(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "ort_ms": round(statistics.fmean(values) * 1000, 1),
        "p95_ms": round(percentile(values, 95) * 1000, 1),
        "maks_ms": round(max(values) * 1000, 1),
        "toplam_sn": round(sum(values), 2),
    }


async def table_counters(conn) -> dict:
    rows = await conn.fetch(
        "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del, pg_total_relation_size(relid) AS bytes "
        "FROM pg_stat_user_tables WHERE relname = ANY($1::text[])", list(META_TABLES))
    return {r["relname"]: dict(r) for r in rows}


async def run(args) -> dict:
    import asyncpg

    await prepare_meta()

    proxies: list[subprocess.Popen] = []
    proxy_map: dict[int, int] = {}
    if args.meta_rtt_ms or args.target_rtt_ms:
        pairs_meta = [f"{META_PROXY_PORT}={META_PORT}"] if args.meta_rtt_ms else []
        if pairs_meta:
            proxies.append(subprocess.Popen([sys.executable, str(BACKEND / "scripts" / "tcp_delay_proxy.py"),
                                             "--rtt-ms", str(args.meta_rtt_ms), *pairs_meta], stdout=subprocess.PIPE))
        if args.target_rtt_ms:
            real_ports = [t.port for _, _, t in targets_catalog()]
            pairs = []
            for index, port in enumerate(real_ports):
                proxy_map[port] = TARGET_PROXY_BASE + index
                pairs.append(f"{TARGET_PROXY_BASE + index}={port}")
            proxies.append(subprocess.Popen([sys.executable, str(BACKEND / "scripts" / "tcp_delay_proxy.py"),
                                             "--rtt-ms", str(args.target_rtt_ms), *pairs], stdout=subprocess.PIPE))
        for proxy in proxies:
            ready = proxy.stdout.readline().decode("utf-8").strip()
            if ready != "hazır":  # port başka bir süreçte açık olabilir: sessizce eski vekille ölçmeyelim
                for other in proxies:
                    other.terminate()
                raise SystemExit(f"gecikme vekili başlamadı (port dolu mu?): {ready!r}")

    meta_port = META_PROXY_PORT if args.meta_rtt_ms else META_PORT
    os.environ["DATABASE_URL"] = f"postgresql://postgres:dbace@127.0.0.1:{meta_port}/{DATABASE}"
    os.environ["WAIT_SAMPLE_INTERVAL_SECONDS"] = str(args.interval)

    ids = await register_instances(args.instances, args.target_rtt_ms, proxy_map, args.only)

    workload = None
    if not args.no_workload:
        pg_dsns = ",".join(f"postgresql://postgres:dbace@127.0.0.1:{p}/dbace" for p in PG_PORTS)
        workload = subprocess.Popen(
            [sys.executable, str(BACKEND / "scripts" / "sampler_workload.py"), "--pg", pg_dsns, "--mssql",
             "--seconds", str(args.seconds + 20), "--workers", str(args.workers)], stdout=subprocess.PIPE, text=True)
        workload.stdout.readline()
        await asyncio.sleep(3)  # yük otursun; ısınma turları ölçüme girmiyor

    if args.default_executor_workers:
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=args.default_executor_workers))

    import logging

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(f"{record.levelname}: {record.getMessage()}")

    handler = _Capture(level=logging.INFO)
    logging.getLogger("app.services.wait_sampling").addHandler(handler)
    logging.getLogger("app.services.wait_sampling").setLevel(logging.INFO)

    recorder = Recorder()
    install_wrappers(recorder, {})
    from app.services import wait_sampling as _ws_for_summary
    _ws_for_summary.SUMMARY_LOG_INTERVAL_SECONDS = 60.0  # özet satırı ölçümde de görünsün
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.collectors import scheduler as scheduler_module
    from app.services import wait_sampling

    wait_sampling.reset_state()
    scheduler = AsyncIOScheduler()
    scheduler.add_listener(lambda event: setattr(recorder, "skipped_by_scheduler", recorder.skipped_by_scheduler + 1),
                           EVENT_JOB_MAX_INSTANCES)

    original_tick = scheduler_module.wait_sampling_tick

    running = {"n": 0}  # süren iş sayısı: kapatmadan önce bitmelerini bekleyeceğiz (iptal, havuzdaki bağlantıyı bozar)

    async def timed_tick():
        started = time.monotonic()
        running["n"] += 1
        try:
            await original_tick()
        finally:
            running["n"] -= 1
            recorder.ticks.append(time.monotonic() - started)

    if hasattr(scheduler_module, "wait_flush_tick"):
        original_flush = scheduler_module.wait_flush_tick

        async def timed_flush():
            running["n"] += 1
            try:
                await original_flush()
            finally:
                running["n"] -= 1

        scheduler_module.wait_flush_tick = timed_flush

    scheduler_module.wait_sampling_tick = timed_tick
    if hasattr(scheduler_module, "register_wait_sampling_jobs"):
        scheduler_module.register_wait_sampling_jobs(scheduler)
    else:  # eski sürüm: üretimdeki satır içi kayıtla aynı parametreler
        from datetime import datetime
        scheduler.add_job(timed_tick, "interval", seconds=max(1, args.interval), id="wait_event_sampling",
                          max_instances=1, coalesce=True, next_run_time=datetime.now())

    meta = await asyncpg.connect(f"{ADMIN_DSN}/{DATABASE}")
    before = await table_counters(meta)
    tx_before = await meta.fetchrow("SELECT tup_inserted, tup_updated FROM pg_stat_database WHERE datname = $1", DATABASE)

    started_wall = time.monotonic()
    scheduler.start()
    write_window = float(args.seconds)
    if args.warmup:
        # Kararlı hâl: ısınma (imza sözlüğü dolumu, ilk kova) sayılmasın; satır/saat yalnızca sonrasından hesaplanır.
        await asyncio.sleep(args.warmup)
        before = await table_counters(meta)
        write_window = float(args.seconds - args.warmup)
    await asyncio.sleep(args.seconds - args.warmup)
    elapsed = time.monotonic() - started_wall
    scheduler.pause()
    drain_started = time.monotonic()
    while running["n"] and time.monotonic() - drain_started < 90:
        await asyncio.sleep(0.2)
    scheduler.shutdown(wait=False)
    status = wait_sampling.sampling_status()

    async with wait_sampling.SessionLocal() as session:
        await wait_sampling.flush_all_buckets(session)
        await session.commit()
    await wait_sampling.shutdown_sampling()
    after = await table_counters(meta)
    tx_after = await meta.fetchrow("SELECT tup_inserted, tup_updated FROM pg_stat_database WHERE datname = $1", DATABASE)
    minute_rows = await meta.fetch(
        "SELECT instance_id, count(*) AS minutes, sum(samples_taken) AS samples FROM active_session_minutes GROUP BY 1")
    await meta.close()

    if workload:
        workload.terminate()
    for proxy in proxies:
        proxy.terminate()

    # Gerçek örnekleme aralığı: instance başına (collector nesnesi başına) ardışık geliş farkları.
    gaps: list[float] = []
    per_instance_mean = []
    for stamps in recorder.completed.values():
        diffs = [b - a for a, b in zip(stamps, stamps[1:])]
        gaps.extend(diffs)
        if diffs:
            per_instance_mean.append(statistics.fmean(diffs))
    expected_slots = max(1, int(elapsed / max(1, args.interval))) * max(1, len(recorder.completed))
    taken = sum(len(v) for v in recorder.completed.values())
    interval_s = max(1, args.interval)
    missed_slots = sum(max(0, round(g / interval_s) - 1) for g in gaps)

    written = {t: {k: after[t][k] - before.get(t, {}).get(k, 0) for k in ("n_tup_ins", "n_tup_upd", "bytes")}
               for t in after}
    hours = write_window / 3600.0
    result = {
        "etiket": args.label, "instance": args.instances, "sunucu_sayisi": min(args.instances, len(targets_catalog())),
        "sure_sn": round(elapsed, 1), "hedef_aralik_sn": interval_s,
        "hedef_rtt_ms": args.target_rtt_ms, "meta_rtt_ms": args.meta_rtt_ms,
        "gercek_aralik": {
            "ort_ms": round(statistics.fmean(gaps) * 1000, 1) if gaps else None,
            "p95_ms": round(percentile(gaps, 95) * 1000, 1), "maks_ms": round(max(gaps) * 1000, 1) if gaps else None,
            "ornek_sayisi": taken, "beklenen_ornek": expected_slots,
            "kayip_yuzde": round(100 * (1 - taken / expected_slots), 1),
            "araliklardan_turetilen_atlanan_tur": missed_slots,
        },
        "scheduler_atlanan_tur": recorder.skipped_by_scheduler,
        "tur_suresi": summarize_durations(recorder.ticks),
        "bilesenler": {k: summarize_durations(v) for k, v in sorted(recorder.durations.items())},
        "meta_gidis_donus": {"sorgu": recorder.meta_statements, "commit": recorder.meta_commits,
                             "toplam_sn": round(recorder.meta_seconds, 2)},
        "meta_yazim_olculen": written,
        "meta_yazim_saat_basi_satir": {t: round((v["n_tup_ins"] + v["n_tup_upd"]) / hours) for t, v in written.items()},
        "meta_yazim_saat_basi_ekleme": {t: round(v["n_tup_ins"] / hours) for t, v in written.items()},
        "meta_yazim_saat_basi_bayt": {t: round(v["bytes"] / hours) for t, v in written.items()},
        "dakika_kayitlari": {int(r["instance_id"]): {"dakika": r["minutes"], "ornek": int(r["samples"] or 0)} for r in minute_rows},
        "ornek_sayisi_instance_basina_dakikada": round(taken / max(1, len(recorder.completed)) / (elapsed / 60), 1),
        "sampling_status": {k: status.get(k) for k in ("measured_interval_ms", "missed_slots", "skipped_in_flight",
                                                       "cadence_message", "instances_sampled", "instances_failing")},
        "ozet_loglari": captured[-3:],
        "varsayilan_havuz": args.default_executor_workers or "varsayılan",
        "motor_filtresi": args.only or "karışık",
    }
    return result


def render(result: dict) -> str:
    lines = [f"### {result['etiket']} — {result['instance']} instance, {result['sure_sn']} sn, "
             f"hedef RTT {result['hedef_rtt_ms']} ms, meta RTT {result['meta_rtt_ms']} ms", ""]
    g = result["gercek_aralik"]
    lines += [f"- **gerçek aralık** (hedef {result['hedef_aralik_sn'] * 1000} ms): ort {g['ort_ms']} ms, "
              f"p95 {g['p95_ms']} ms, maks {g['maks_ms']} ms; alınan {g['ornek_sayisi']} / beklenen {g['beklenen_ornek']} "
              f"(kayıp %{g['kayip_yuzde']}), aralıklardan atlanan tur {g['araliklardan_turetilen_atlanan_tur']}",
              f"- **scheduler'ın atladığı tur:** {result['scheduler_atlanan_tur']}",
              f"- **tur süresi:** {result['tur_suresi']}", "", "| bileşen | n | ort ms | p95 ms | maks ms | toplam sn |", "|---|---|---|---|---|---|"]
    for key, s in result["bilesenler"].items():
        lines.append(f"| {key} | {s.get('n')} | {s.get('ort_ms')} | {s.get('p95_ms')} | {s.get('maks_ms')} | {s.get('toplam_sn')} |")
    lines += ["", f"- sampling_status: {result.get('sampling_status')}"]
    for message in result.get("ozet_loglari", []):
        lines.append(f"- özet log: {message}")
    m = result["meta_gidis_donus"]
    lines += ["", f"- meta gidiş-dönüş: {m['sorgu']} sorgu, {m['commit']} commit, toplam {m['toplam_sn']} sn",
              f"- meta yazım (satır/saat, ekleme+güncelleme): {result['meta_yazim_saat_basi_satir']}",
              f"- meta yazım (ekleme/saat): {result['meta_yazim_saat_basi_ekleme']}",
              f"- meta yazım (bayt/saat, tablo boyutu farkı): {result['meta_yazim_saat_basi_bayt']}"]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--interval", type=int, default=1)
    parser.add_argument("--target-rtt-ms", type=float, default=0)
    parser.add_argument("--meta-rtt-ms", type=float, default=0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--no-workload", action="store_true")
    parser.add_argument("--warmup", type=int, default=0, help="ısınma saniyesi; yazım hacmi bundan sonrasından hesaplanır")
    parser.add_argument("--only", choices=["pg", "mssql"], default="", help="yalnızca bu motorun instance'ları")
    parser.add_argument("--default-executor-workers", type=int, default=0,
                        help="varsayılan iş parçacığı havuzunu bu boyuta indirir (küçük bir worker'ı taklit eder)")
    parser.add_argument("--label", default="olcum")
    parser.add_argument("--out", default="")
    parsed = parser.parse_args()
    outcome = asyncio.run(run(parsed))
    print(render(outcome), flush=True)
    if parsed.out:
        Path(parsed.out).write_text(json.dumps(outcome, ensure_ascii=False, indent=1), encoding="utf-8")
    os._exit(0)  # sürücülerin arka plan iş parçacıkları süreci tutmasın
