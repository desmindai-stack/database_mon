"""Temizlik migration'ının (#48) GERÇEK PostgreSQL'de çalıştırılarak doğrulanması (Faz 31 Commit 4).

Migration dosyasının KENDİSİ çalıştırılıyor — elle kopyalanmış bir SQL değil. dbace'in meta veri
şeması ayrı bir veritabanında uygulamanın kendi modellerinden (`Base.metadata.create_all`) kuruluyor,
gerçek değer taşıyan satırlar yazılıyor, migration koşuyor ve sonuç Python tarafının
(`normalize_literals`, `strip_plan_values`) çıktısıyla BİREBİR karşılaştırılıyor. İki taraf farklı
sonuç üretirse yerelde (Python) ve canlıda (migration) aynı veri farklı görünürdü.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.sql_analysis import normalize_literals, strip_plan_values
from tests.live_pg import LIVE_DSNS, SKIP_REASON, dsn_id, with_database

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "20260916090800_real_value_cleanup.sql"
META_DATABASE = "dbace_meta_it"

CORPUS = [
    "SELECT * FROM users WHERE tc = '12345678901' AND age > 30 AND t1.col2 = 1.5",
    "SELECT pg_sleep(1.0), 'it''s' FROM t WHERE a = $1 AND b = 7",
    "SELECT * FROM orders WHERE note = N'Ayşe' AND total = -12.5e3 LIMIT 10",
    "SELECT id FROM t WHERE ts > now() - interval '5 minutes'",
    "/* dbace */ EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT count(*) FROM orders WHERE status = 'secret-slow-1'",
    "SELECT 1",
    "",
    "SELECT x FROM t_2024 WHERE iban = 'TR00 0000' AND n IN (1, 22, 333)",
]

PLAN = {
    "Query Text": "SELECT * FROM customers WHERE tc_no = '12345678901' AND balance > 1500.75",
    "Plan": {
        "Node Type": "Index Scan", "Relation Name": "customers_2024", "Index Name": "idx_customers_tc_2",
        "Index Cond": "(tc_no = '12345678901'::text)", "Filter": "(balance > 1500.75)",
        "Plan Rows": 1, "Total Cost": 8.44, "Actual Rows": 1,
        "Plans": [{"Node Type": "Seq Scan", "Output": ["id", "(n + 1)"], "Sort Key": ["created_at DESC"]}],
    },
}


@pytest.fixture(params=LIVE_DSNS, ids=dsn_id)
def dsn(request):
    return request.param


@pytest.fixture
async def meta(dsn):
    """dbace meta veri şeması, uygulamanın modellerinden, gerçek PostgreSQL'de."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base

    admin = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", META_DATABASE):
            await admin.execute(f"CREATE DATABASE {META_DATABASE}")
    finally:
        await admin.close()
    url = with_database(dsn, META_DATABASE).replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    conn = await asyncpg.connect(with_database(dsn, META_DATABASE), statement_cache_size=0)
    await conn.execute(
        "TRUNCATE wait_query_signatures, captured_plans, slow_query_samples, app_settings RESTART IDENTITY CASCADE"
    )
    try:
        yield conn
    finally:
        await conn.close()


async def _instance_id(conn) -> int:
    existing = await conn.fetchval("SELECT id FROM instances WHERE name = 'cleanup-it'")
    if existing:
        return existing
    return await conn.fetchval(
        "INSERT INTO instances (name, engine, host, port, database, username, password, enabled, environment) "
        "VALUES ('cleanup-it', 'postgresql', 'h', 5432, 'd', 'u', 'x', true, 'public') RETURNING id"
    )


async def _seed(conn, *, store_real: bool) -> int:
    instance = await _instance_id(conn)
    if store_real:
        await conn.execute("INSERT INTO app_settings (key, value) VALUES ('analysis_store_real_query_samples', 'true')")
    now = datetime.now(UTC)
    for i, text in enumerate(CORPUS):
        await conn.execute(
            "INSERT INTO wait_query_signatures (instance_id, queryid, query_text, sample_query_text, sample_duration_ms, "
            "sample_captured_at, seen_bind_parameters) VALUES ($1, $2, $3, $4, 1200, $5, false)",
            instance, f"q{i}", text, text or None, now,
        )
    await conn.execute(
        "INSERT INTO captured_plans (instance_id, captured_at, source, duration_ms, query_text, query_fingerprint, "
        "has_actual_rows, plan_json) VALUES ($1, $2, 'auto_explain', 12.5, $3, 'fp', true, $4::json)",
        instance, now, PLAN["Query Text"], json.dumps(PLAN),
    )
    rows = [
        CORPUS[4],  # dbace'in imzalı EXPLAIN'i — değerli
        "EXPLAIN (ANALYZE) SELECT * FROM orders WHERE status = 'eski-elle-analyze'",  # eski, imzasız
        "SELECT * FROM orders WHERE status = $1",  # normal uygulama satırı — dokunulmamalı
        "SET application_name = 'uygulama-2'",  # EXPLAIN değil — bu migration'ın kapsamı dışı
    ]
    for text in rows:
        await conn.execute(
            "INSERT INTO slow_query_samples (instance_id, collected_at, query, calls, total_time_ms, mean_time_ms, rows) "
            "VALUES ($1, $2, $3, 1, 1, 1, 1)",
            instance, now, text,
        )
    return instance


async def _run_migration(conn) -> None:
    """Migration'ı UYGULAYICIYLA çalıştır (Commit 10b: parçalı ifadeler `$1/$2` parametresi ister ve işlem dışında,
    tek bağlantıda koşar — pg_temp işlevleri oturuma ait). Kayıt tablosuna dokunulmuyor: dosya her seferinde çalışır."""
    from app.migrations_runner import apply_migrations

    applied = await apply_migrations(conn, MIGRATION.parent, only=MIGRATION.name, record=False)
    assert applied == [MIGRATION.name]


async def _snapshot(conn):
    return (
        [dict(r) for r in await conn.fetch(
            "SELECT queryid, query_text, sample_query_text, sample_duration_ms FROM wait_query_signatures ORDER BY queryid"
        )],
        [dict(r) for r in await conn.fetch("SELECT query_text, plan_json::text AS plan FROM captured_plans")],
        [r["query"] for r in await conn.fetch("SELECT query FROM slow_query_samples ORDER BY id")],
    )


async def test_sql_normalizer_is_identical_to_python(meta):
    body = MIGRATION.read_text(encoding="utf-8")
    functions = body[body.index("CREATE OR REPLACE FUNCTION pg_temp.dbace_strip_literals"): body.index("-- 1. Sözlük metni")]
    await meta.execute(functions)
    for text in CORPUS:
        assert await meta.fetchval("SELECT pg_temp.dbace_strip_literals($1)", text) == normalize_literals(text), text
    sql_plan = json.loads(await meta.fetchval("SELECT pg_temp.dbace_strip_plan($1::jsonb)::text", json.dumps(PLAN)))
    assert sql_plan == strip_plan_values(PLAN)


async def test_migration_with_switch_off_strips_everything_and_is_idempotent(meta, dsn):
    await _seed(meta, store_real=False)
    await _run_migration(meta)
    signatures, plans, samples = await _snapshot(meta)
    version = (await meta.fetchval("SHOW server_version")).split(" ")[0]
    print(f"\n  [{version}] imza metinleri={[s['query_text'] for s in signatures][:3]} …")
    print(f"  [{version}] plan Filter={json.loads(plans[0]['plan'])['Plan']['Filter']!r} yavaş sorgu={samples}")

    for row, original in zip(signatures, CORPUS):
        assert row["query_text"] == normalize_literals(original)
        assert row["sample_query_text"] is None and row["sample_duration_ms"] is None
    assert plans[0]["query_text"] == normalize_literals(PLAN["Query Text"])
    assert json.loads(plans[0]["plan"]) == strip_plan_values(PLAN)
    assert samples[0] == normalize_literals(CORPUS[4]) and "secret" not in samples[0]
    assert samples[1] == normalize_literals(samples[1]) and "eski-elle-analyze" not in samples[1]
    assert samples[2] == "SELECT * FROM orders WHERE status = $1"
    assert samples[3] == "SET application_name = 'uygulama-2'", "kapsam dışı satır değişmemeli"

    before = await _snapshot(meta)
    await _run_migration(meta)
    assert await _snapshot(meta) == before, "migration idempotent değil"


async def test_migration_with_switch_on_keeps_samples_and_plans_but_still_strips_dictionary_text(meta):
    await _seed(meta, store_real=True)
    await _run_migration(meta)
    signatures, plans, _ = await _snapshot(meta)
    for row, original in zip(signatures, CORPUS):
        assert row["query_text"] == normalize_literals(original)
        assert row["sample_query_text"] == (original or None)
    assert plans[0]["query_text"] == PLAN["Query Text"]
    assert json.loads(plans[0]["plan"]) == PLAN
