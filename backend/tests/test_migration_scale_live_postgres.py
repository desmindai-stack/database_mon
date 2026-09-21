"""Migration'lar GERÇEK ÖLÇEKTE (≥ 400 bin satır) gerçek PostgreSQL'de (Faz 31 Commit 10b).

Arka plan: #53 (`slow_query_sample_identity`) canlıda 393 bin satırlık `slow_query_samples`'ı tek UPDATE ile doldurup
index'i düz `CREATE INDEX` ile kurdu: Supabase'de ifade zaman aşımına uğradı ve TÜMÜYLE geri alındı.

Ölçek verisi BİR KEZ üretilir ve kalıcı bir şablon veritabanında (`dbace_migscale_base`) durur; her test ondan kopya alır.
Şablon en eski migration'ların şemasıyla kurulur (tablolar o günkü hâlleriyle), sonra TÜM migration'lar sırayla
uygulanır — yani "yıllık eski bir kurulumu bugüne yükseltme" senaryosu, gerçek uygulayıcıyla. Bağlantıya yönetilen
veritabanının ifade sınırı taklit edilir (`statement_timeout = 8s`, Supabase'in varsayılanı).

Ölçülenler:
1. Tüm migration'lar 8 sn ifade sınırıyla bitiyor; en uzun parça/ifade süresi.
2. Sonuç DOĞRU: parmak izleri Python'la aynı, index'ler geçerli, FK doğrulanmış, gerçek değerler temizlenmiş.
3. Yeniden çalıştırma idempotent (0 satır güncellenir); yarıda kesilen backfill kalınan yerden tamamlanıyor ve
   kesintisiz çalıştırmayla AYNI sonucu veriyor.
4. NEGATİF KONTROL: #53'ün ilk sürümü (tek UPDATE) aynı sınırla aynı veride zaman aşımına uğruyor, yeni sürüm uğramıyor.
5. CONCURRENTLY: index kurulurken yazıcılar bloklanmıyor; düz CREATE INDEX'te bloklanıyor. Yarıda kesilen
   CONCURRENTLY'nin bıraktığı GEÇERSİZ index'i `IF NOT EXISTS` atlıyor — uygulayıcı bunu temizleyip yeniden kuruyor.
"""

from __future__ import annotations

import asyncio

import statistics
import time
from pathlib import Path

import pytest

from app.migrations_runner import StatementStat, apply_migrations
from app.services.slow_query_selection import query_fingerprint
from tests.live_pg import LIVE_DSNS, SKIP_REASON, with_database

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
ROWS = 420_000
BASE = "dbace_migscale_base"
UPGRADED = "dbace_migscale_up"
PRE53 = "dbace_migscale_pre53"
STAMP_VERSION = 2

FIRST_HEAVY = "20250719140000_query_history_index.sql"      # base bu dosyadan ÖNCEKİ şemayla kurulur
STAGE_C = "20260916090800_real_value_cleanup.sql"           # gerçek değerli metin/plan bu dosyadan önce eklenir
IDENTITY = "20260918090000_slow_query_sample_identity.sql"  # #53
STATEMENT_TIMEOUT = "8s"                                    # Supabase varsayılanı (yönetilen veritabanı sınırı)

SECRET = "SECRET-LIT-777"
CACHE: dict = {}


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


def admin_dsn() -> str:
    return with_database(LIVE_DSNS[0], "postgres")


def dsn_for(database: str) -> str:
    return with_database(LIVE_DSNS[0], database)


async def _admin():
    return await asyncpg.connect(admin_dsn(), statement_cache_size=0)


async def _drop(name: str) -> None:
    admin = await _admin()
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    finally:
        await admin.close()


async def _clone(source: str, name: str) -> None:
    admin = await _admin()
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {name} TEMPLATE {source}")
    finally:
        await admin.close()


async def _connect(database: str):
    return await asyncpg.connect(dsn_for(database), statement_cache_size=0)


# --- Ölçek verisi (bir kez) --------------------------------------------------------------------

_SLOW_QUERY_TEXT = """
    'SELECT o.id, o.customer_id, o.status, o.total, o.created_at, c.name, c.segment /* rapor ' || (g % 200) || ' */
        FROM orders_' || (g % 200) || ' o JOIN customers c ON c.id = o.customer_id
       WHERE o.status = $1 AND o.created_at >= $2 ' || repeat('AND o.flag_' || (g % 200) || ' IS NOT NULL ', 18)
    || 'ORDER BY o.created_at DESC LIMIT $3'"""


async def _seed_base(conn) -> dict:
    """En eski şemayla kurulmuş şablona büyük tablolar: slow_query_samples, alert_events, metric_samples (her biri ROWS)."""
    started = time.monotonic()
    await conn.execute("INSERT INTO instances (name, host, username, password) VALUES ('scale', 'h', 'u', 'p')")
    await conn.execute("INSERT INTO alert_rules (instance_id, name, metric, operator, threshold) VALUES (1, 'r', 'm', '>', 1)")
    # Farklı boşluk/büyük-küçük harf biçimleri: parmak izi normalleştirmesini zorlar. Her 1000. satır EXPLAIN (gerçek değerli).
    await conn.execute(f"""
        INSERT INTO slow_query_samples (instance_id, collected_at, queryid, query, calls, total_time_ms, mean_time_ms, rows)
        SELECT 1, now() - (g || ' seconds')::interval, (1000 + g % 200)::text,
               CASE WHEN g % 1000 = 0 THEN 'EXPLAIN (ANALYZE) SELECT * FROM t WHERE tc = ''{SECRET}-' || g || ''''
                    WHEN g % 5 = 0 THEN upper({_SLOW_QUERY_TEXT})
                    WHEN g % 5 = 1 THEN regexp_replace({_SLOW_QUERY_TEXT}, ' ', E'\\n   ', 'g')
                    ELSE {_SLOW_QUERY_TEXT} END,
               100 + g % 50, (g % 900) * 1.5, (g % 90) * 1.1, g % 1000
          FROM generate_series(1, {ROWS}) g""")
    await conn.execute(f"""
        INSERT INTO alert_events (rule_id, instance_id, metric_value, message, triggered_at)
        SELECT 1, 1, g, 'uyarı ' || g, now() - (g || ' seconds')::interval FROM generate_series(1, {ROWS}) g""")
    await conn.execute(f"""
        INSERT INTO metric_samples (instance_id, collected_at, active_connections, max_connections)
        SELECT 1, now() - (g * 15 || ' seconds')::interval, g % 50, 100 FROM generate_series(1, {ROWS}) g""")
    await conn.execute("ANALYZE")
    await conn.execute("CREATE TABLE scale_stamp (version int, rows int)")
    await conn.execute("INSERT INTO scale_stamp VALUES ($1, $2)", STAMP_VERSION, ROWS)
    return {"seed_seconds": round(time.monotonic() - started, 1)}


async def ensure_base() -> dict:
    """Şablon var ve güncelse yeniden kullanılır (BİR KEZ üretilir); değilse kurulur."""
    admin = await _admin()
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", BASE)
    finally:
        await admin.close()
    if exists:
        conn = await _connect(BASE)
        try:
            row = await conn.fetchrow("SELECT version, rows FROM scale_stamp")
            if row and row["version"] == STAMP_VERSION and row["rows"] == ROWS:
                return {"reused": True}
        except asyncpg.PostgresError:
            pass
        finally:
            await conn.close()
    admin = await _admin()
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {BASE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {BASE}")
    finally:
        await admin.close()
    conn = await _connect(BASE)
    try:
        await apply_migrations(conn, MIGRATIONS, until=FIRST_HEAVY)
        info = await _seed_base(conn)
    finally:
        await conn.close()
    return {"reused": False, **info}


async def _seed_stage_c(conn) -> None:
    """Migration #48'den önce: gerçek değer taşıyan imza metinleri/örnekleri ve auto_explain planları."""
    await conn.execute(f"""
        INSERT INTO wait_query_signatures (instance_id, queryid, query_text, sample_query_text, sample_duration_ms, sample_captured_at)
        SELECT 1, g::text, 'SELECT * FROM users WHERE tc = ''{SECRET}-' || g || ''' AND age > ' || (g % 80),
               CASE WHEN g % 2 = 0 THEN 'SELECT * FROM users WHERE tc = ''{SECRET}-' || g || '''' END,
               CASE WHEN g % 2 = 0 THEN 12.5 END, CASE WHEN g % 2 = 0 THEN now() END
          FROM generate_series(1, 60000) g""")
    await conn.execute(f"""
        INSERT INTO captured_plans (instance_id, captured_at, duration_ms, query_text, query_fingerprint, plan_json)
        SELECT 1, now() - (g || ' seconds')::interval, 5 + g % 100,
               'SELECT * FROM users WHERE tc = ''{SECRET}-' || g || '''', 'fp' || (g % 500),
               jsonb_build_object('Query Text', 'SELECT * FROM users WHERE tc = ''{SECRET}-' || g || '''',
                 'Plan', jsonb_build_object('Node Type', 'Seq Scan', 'Relation Name', 'users',
                     'Filter', '(tc = ''{SECRET}-' || g || '''::text)',
                     'Plans', (SELECT jsonb_agg(jsonb_build_object('Node Type', 'Index Scan', 'Index Cond', '(id = ' || (g + k) || ')'))
                                 FROM generate_series(1, 8) k)))
          FROM generate_series(1, 20000) g""")


async def _upgraded() -> dict:
    """Şablondan kopya + TÜM migration'lar (8 sn ifade sınırıyla); sonuçlar önbelleğe."""
    if "upgraded" in CACHE:
        return CACHE["upgraded"]
    base = await ensure_base()
    await _clone(BASE, UPGRADED)
    conn = await _connect(UPGRADED)
    stats: list[StatementStat] = []
    per_file: dict[str, float] = {}
    started_all = time.monotonic()
    pre53_ready = False
    try:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < FIRST_HEAVY:
                continue
            if path.name == STAGE_C:
                await _seed_stage_c(conn)
            if path.name == IDENTITY and not pre53_ready:
                await conn.close()
                await _clone(UPGRADED, PRE53)  # #53'ten ÖNCEKİ hâl: negatif kontroller ve kesinti testleri buradan
                conn = await _connect(UPGRADED)
                await conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
                pre53_ready = True
            started = time.monotonic()
            await apply_migrations(conn, MIGRATIONS, only=path.name, statement_timeout=STATEMENT_TIMEOUT, stats=stats)
            per_file[path.name] = time.monotonic() - started
        total = time.monotonic() - started_all
        checksum = await conn.fetchval("SELECT md5(string_agg(query_hash, ',' ORDER BY id)) FROM slow_query_samples")
    finally:
        await conn.close()
    CACHE["upgraded"] = {"base": base, "stats": stats, "per_file": per_file, "total": total, "checksum": checksum}
    return CACHE["upgraded"]


def _chunks(stats: list[StatementStat], file: str | None = None) -> list[StatementStat]:
    return [s for s in stats if s.kind == "chunk" and (file is None or s.file == file)]


# --- 1. Tüm migration'lar yönetilen veritabanının ifade sınırıyla bitiyor ------------------------


async def test_all_migrations_finish_within_the_managed_statement_timeout_on_400k_rows():
    run = await _upgraded()
    per_file = sorted(run["per_file"].items(), key=lambda kv: -kv[1])
    chunks = _chunks(run["stats"])
    slowest_statement = max((s for s in run["stats"] if s.kind == "statement"), key=lambda s: s.seconds)
    log("ölçek verisi", run["base"])
    log("toplam yükseltme süresi (sn)", round(run["total"], 1))
    log("en uzun 6 dosya (sn)", [(n, round(t, 1)) for n, t in per_file[:6]])
    log("parça sayısı / ort / p95 / en uzun (sn)", (len(chunks), round(statistics.mean(c.seconds for c in chunks), 2),
        round(sorted(c.seconds for c in chunks)[int(len(chunks) * 0.95)], 2), round(max(c.seconds for c in chunks), 2)))
    from app.migration_sql import large_tables, long_running_tables

    long_files = {p.name: sorted(long_running_tables(p.read_text(encoding="utf-8"), large_tables()))
                  for p in sorted(MIGRATIONS.glob("*.sql")) if long_running_tables(p.read_text(encoding="utf-8"), large_tables())}
    log("UZUN SÜREN dosyalar (sn, %d satırlık tablolarla)" % ROWS, {n: (round(run["per_file"].get(n, 0), 1), t) for n, t in long_files.items()})
    log("en uzun tek ifade", (slowest_statement.file, slowest_statement.text[:60], round(slowest_statement.seconds, 2)))
    # 8 sn sınırıyla bitti (aksi hâlde yukarıda QueryCanceledError). Parça başına pay: sınırın yarısından az.
    assert max(c.seconds for c in chunks) < 4.0
    assert slowest_statement.seconds < 8.0


async def test_the_identity_backfill_runs_as_20k_chunks_over_400k_rows():
    run = await _upgraded()
    chunks = _chunks(run["stats"], IDENTITY)
    conn = await _connect(UPGRADED)
    try:
        lo, hi = await conn.fetchval("SELECT min(id) FROM slow_query_samples"), await conn.fetchval("SELECT max(id) FROM slow_query_samples")
    finally:
        await conn.close()
    assert len(chunks) == (hi - lo) // 20_000 + 1 and len(chunks) >= 20
    assert sum(c.rows for c in chunks) >= ROWS, "her satır güncellenmeli"
    log("#53 parçaları", (len(chunks), [round(c.seconds, 2) for c in chunks]))


# --- 2. Sonuç doğru ------------------------------------------------------------------------------


async def test_result_is_correct_hashes_match_python_indexes_valid_fk_validated_values_cleaned():
    await _upgraded()
    conn = await _connect(UPGRADED)
    try:
        assert await conn.fetchval("SELECT count(*) FROM slow_query_samples WHERE query_hash IS NULL") == 0
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM slow_query_samples TABLESAMPLE SYSTEM (0.3) LIMIT 400")]
        assert len(ids) > 100
        rows = await conn.fetch("SELECT query, query_hash FROM slow_query_samples WHERE id = ANY($1::bigint[])", ids)
        mismatches = [r["query"][:60] for r in rows if r["query_hash"] != query_fingerprint(r["query"])]
        assert mismatches == [], "SQL parmak izi Python'dan ayrışmış"
        # NEGATİF KONTROL: satırlar gerçekten farklı biçimlerde (büyük harf / satır sonu) — normalleştirme sınanıyor.
        assert any(r["query"].isupper() for r in rows) and any("\n" in r["query"] for r in rows)

        assert await conn.fetchval("SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
                                   "WHERE NOT i.indisvalid AND c.relnamespace = 'public'::regnamespace") == 0, "geçersiz index kalmış"
        for index in ("ix_slow_query_samples_instance_hash", "idx_slow_query_samples_instance_queryid_collected",
                      "idx_alert_events_group_id", "ix_metric_samples_instance_collected"):
            assert await conn.fetchval("SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid = to_regclass($1)", index), index
        assert await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE conname = 'alert_events_group_id_fkey'") is True

        # #48: gerçek değerler temizlendi (ayar kapalı).
        assert await conn.fetchval(f"SELECT count(*) FROM slow_query_samples WHERE query LIKE '%{SECRET}%'") == 0
        assert await conn.fetchval(f"SELECT count(*) FROM wait_query_signatures WHERE query_text LIKE '%{SECRET}%' OR sample_query_text IS NOT NULL") == 0
        assert await conn.fetchval(f"SELECT count(*) FROM captured_plans WHERE query_text LIKE '%{SECRET}%' OR plan_json::text LIKE '%{SECRET}%'") == 0
        assert await conn.fetchval("SELECT count(*) FROM captured_plans") == 20_000 and await conn.fetchval("SELECT count(*) FROM alert_events") == ROWS
    finally:
        await conn.close()


# --- 3. Idempotent, yeniden çalıştırılabilir, kesintiden sonra aynı sonuç ----------------------------


async def test_rerunning_the_heavy_migrations_is_idempotent_and_touches_no_rows():
    await _upgraded()
    conn = await _connect(UPGRADED)
    try:
        assert await apply_migrations(conn, MIGRATIONS) == [], "kayıtlı migration'lar bir daha uygulanmaz"
        stats: list[StatementStat] = []
        for name in (IDENTITY, STAGE_C, "20250719140000_query_history_index.sql", "20260825130000_node_credentials_and_group_alerts.sql"):
            await apply_migrations(conn, MIGRATIONS, only=name, record=False, statement_timeout=STATEMENT_TIMEOUT, stats=stats)
        touched = sum(s.rows for s in stats if s.kind == "chunk")
        log("yeniden çalıştırmada güncellenen satır", touched)
        assert touched == 0, "idempotent olmalı: yapılmış parçalar 0 satır güncellemeli"
        assert await conn.fetchval("SELECT count(*) FROM pg_index WHERE NOT indisvalid") == 0
    finally:
        await conn.close()


class InterruptingConnection:
    """Gerçek bağlantıyı sarar; parametreli (parçalı) ifadelerden N'incisinde bağlantı kopmuş gibi hata verir."""

    def __init__(self, conn, fail_at: int) -> None:
        self._conn, self.fail_at, self.seen = conn, fail_at, 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args):
        if args and sql.lstrip().upper().startswith("UPDATE"):
            self.seen += 1
            if self.seen == self.fail_at:
                raise ConnectionResetError("bağlantı koptu (test)")
        return await self._conn.execute(sql, *args)


async def test_an_interrupted_backfill_resumes_and_ends_identical_to_an_uninterrupted_run():
    run = await _upgraded()
    await _clone(PRE53, "dbace_migscale_resume")
    conn = await _connect("dbace_migscale_resume")
    try:
        with pytest.raises(ConnectionResetError):
            await apply_migrations(InterruptingConnection(conn, fail_at=4), MIGRATIONS, only=IDENTITY)
        done = await conn.fetchval("SELECT count(*) FROM slow_query_samples WHERE query_hash IS NOT NULL")
        left = await conn.fetchval("SELECT count(*) FROM slow_query_samples WHERE query_hash IS NULL")
        assert 0 < done < ROWS and left > 0, "yarıda kalmış olmalı (3 parça bitti)"
        assert IDENTITY not in {r["version"] for r in await conn.fetch("SELECT version FROM dbace_meta.applied_migrations")}, \
            "yarım dosya kayda geçmemeli"
        stats: list[StatementStat] = []
        await apply_migrations(conn, MIGRATIONS, only=IDENTITY, stats=stats)
        redone = sum(c.rows for c in _chunks(stats, IDENTITY))
        log("kesinti", {"biten satır": done, "kalan": left, "yeniden çalıştırmada güncellenen": redone})
        assert redone == left, "yalnızca kalan satırlar güncellenmeli (biten parçalar atlanır)"
        checksum = await conn.fetchval("SELECT md5(string_agg(query_hash, ',' ORDER BY id)) FROM slow_query_samples")
        assert checksum == run["checksum"], "kesintili ve kesintisiz sonuç aynı olmalı"
    finally:
        await conn.close()
        await _drop("dbace_migscale_resume")


# --- 4. NEGATİF KONTROL: eski tek-UPDATE sürümü aynı veride zaman aşımına uğruyor ------------------

OLD_SINGLE_UPDATE = """
UPDATE slow_query_samples
   SET query_hash = 'q:' || substr(encode(sha256(convert_to(
           lower(regexp_replace(btrim(query, E' \\t\\n\\r\\f\\v'), E'[ \\t\\n\\r\\f\\v]+', ' ', 'g')), 'UTF8')), 'hex'), 1, 24)
 WHERE query_hash IS NULL"""


async def test_the_original_single_update_exceeds_the_limit_that_the_chunked_version_meets():
    run = await _upgraded()
    chunk_max = max(c.seconds for c in _chunks(run["stats"], IDENTITY))
    await _clone(PRE53, "dbace_migscale_old")
    conn = await _connect("dbace_migscale_old")
    try:
        await conn.execute("ALTER TABLE slow_query_samples ADD COLUMN query_hash VARCHAR(40)")
        started = time.monotonic()
        await conn.execute(OLD_SINGLE_UPDATE)  # sınırsız (ölçüm)
        single = time.monotonic() - started
        limit = max(2.0, chunk_max * 3)
        log("tek UPDATE süresi vs en uzun parça (sn)", (round(single, 2), round(chunk_max, 2), f"sınır={limit:.1f}"))
        assert single > limit, "bu makinede tek UPDATE, parçalı sürümün karşıladığı sınırı zaten aşıyor olmalı"

        await conn.execute("UPDATE slow_query_samples SET query_hash = NULL")
        await conn.execute(f"SET statement_timeout = '{int(limit * 1000)}ms'")
        with pytest.raises(asyncpg.exceptions.QueryCanceledError):
            await conn.execute(OLD_SINGLE_UPDATE)  # ESKİ davranış: zaman aşımı
        assert await conn.fetchval("SELECT count(*) FROM slow_query_samples WHERE query_hash IS NOT NULL") == 0, \
            "zaman aşımı TÜM işi geri aldı (canlıda yaşanan)"

        await conn.execute("SET statement_timeout = 0")
        await conn.execute("ALTER TABLE slow_query_samples DROP COLUMN query_hash")
        stats: list[StatementStat] = []
        await apply_migrations(conn, MIGRATIONS, only=IDENTITY, statement_timeout=f"{int(limit * 1000)}ms", stats=stats)
        assert sum(c.rows for c in _chunks(stats)) >= ROWS, "aynı sınırla parçalı sürüm tamamlandı"
    finally:
        await conn.close()
        await _drop("dbace_migscale_old")


# --- 5. CONCURRENTLY: yazıcılar bloklanmıyor; geçersiz index kurtarılıyor ---------------------------


async def _writer(database: str, stop: asyncio.Event, latencies: list[float]) -> None:
    conn = await _connect(database)
    try:
        while not stop.is_set():
            started = time.monotonic()
            await conn.execute("INSERT INTO slow_query_samples (instance_id, query, queryid) VALUES (1, 'SELECT writer', 'w')")
            latencies.append(time.monotonic() - started)
            await asyncio.sleep(0.01)
    finally:
        await conn.close()


async def _measure_writers_during(database: str, build) -> tuple[float, float]:
    """`build` çalışırken yazıcı gecikmesinin en büyüğü ve build süresi."""
    stop, latencies = asyncio.Event(), []
    writer = asyncio.create_task(_writer(database, stop, latencies))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await build()
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.2)
    stop.set()
    await writer
    return max(latencies), elapsed


async def test_concurrent_index_build_does_not_block_writers_but_a_plain_build_does():
    await _upgraded()
    await _clone(PRE53, "dbace_migscale_lock")
    conn = await _connect("dbace_migscale_lock")
    try:
        await conn.execute("ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS query_hash VARCHAR(40)")
        await conn.execute("UPDATE slow_query_samples SET query_hash = 'q:' || (id % 5000) WHERE query_hash IS NULL")

        async def plain():
            await conn.execute("CREATE INDEX ix_plain ON slow_query_samples (instance_id, query_hash)")

        plain_latency, plain_build = await _measure_writers_during("dbace_migscale_lock", plain)
        await conn.execute("DROP INDEX ix_plain")

        async def concurrent():
            await conn.execute("CREATE INDEX CONCURRENTLY ix_conc ON slow_query_samples (instance_id, query_hash)")

        conc_latency, conc_build = await _measure_writers_during("dbace_migscale_lock", concurrent)
        log("düz CREATE INDEX: en büyük yazıcı gecikmesi / build (sn)", (round(plain_latency, 2), round(plain_build, 2)))
        log("CONCURRENTLY: en büyük yazıcı gecikmesi / build (sn)", (round(conc_latency, 2), round(conc_build, 2)))
        assert plain_latency > plain_build * 0.5, "düz index kurulurken yazıcı bloklanmalı (negatif kontrol)"
        assert conc_latency < conc_build * 0.3 and conc_latency < 1.0, "CONCURRENTLY yazmayı engellememeli"
    finally:
        await conn.close()
        await _drop("dbace_migscale_lock")


async def test_an_interrupted_concurrent_build_leaves_an_invalid_index_that_the_runner_replaces():
    await _upgraded()
    await _clone(PRE53, "dbace_migscale_invalid")
    conn = await _connect("dbace_migscale_invalid")
    try:
        await conn.execute("ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS query_hash VARCHAR(40)")
        ddl = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_slow_query_samples_instance_hash ON slow_query_samples (instance_id, query_hash)"
        await conn.execute("SET statement_timeout = '30ms'")
        with pytest.raises(asyncpg.exceptions.QueryCanceledError):
            await conn.execute(ddl)
        await conn.execute("SET statement_timeout = 0")
        valid = lambda: conn.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('ix_slow_query_samples_instance_hash')")  # noqa: E731
        assert await valid() is False, "yarıda kesilen CONCURRENTLY geçersiz index bırakmalı"
        await conn.execute(ddl)  # IF NOT EXISTS: atlıyor — index hâlâ geçersiz (sorunun kendisi)
        assert await valid() is False, "düz IF NOT EXISTS geçersiz index'i düzeltmez"

        stats: list[StatementStat] = []
        await apply_migrations(conn, MIGRATIONS, only=IDENTITY, stats=stats, record=False)
        assert await valid() is True, "uygulayıcı geçersiz index'i silip yeniden kurmalı"
    finally:
        await conn.close()
        await _drop("dbace_migscale_invalid")
