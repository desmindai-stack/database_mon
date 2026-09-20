"""Meta veritabanı egress'i — GERÇEK PostgreSQL'de (Faz 31 Commit 9, madde 0).

Canlıda Supabase egress kotası 17 kat aşıldı (85,67 GB / 5 GB). Kök neden: uygulama zaman serisi
tablolarından satırları çekip Python'da işliyordu. Bu dosya düzeltmeyi meta veritabanı PostgreSQL İKEN
ölçüyor — testler normalde SQLite üstünde koşuyor, oysa sorun PostgreSQL'de yaşandı ve yeni yol SQL
(pencere fonksiyonları, `LAG`, JSON çıkarımı) üstüne kurulu:

1. **Eşdeğerlik:** SQL seçimi, AYNI satırlardan hesaplayan referans Python uygulamasıyla birebir aynı
   kalemleri/sayıları veriyor (gruplama, fark, sıfırlanma, sistem filtresi, eşikler).
2. **Ölçüm:** liste/içgörü/teşhis uçları `pg_stat_statements`'ta pencere satırının onda biri kadar bile
   satır döndürmüyor; metin yalnızca sayfadaki kalemler için okunuyor.
3. **Kesinti boşlukları:** PostgreSQL'de SQL'de (`LAG`), SQLite'ta Python'da — iki yol aynı sonucu veriyor.
4. **Migration parmak izi:** #53'ün SQL'de hesapladığı `query_hash`, Python `query_fingerprint` ile aynı.
5. **pg_timezone_names:** uygulamanın hiçbir yolu çağırmıyor (canlıdaki çağrılar dbace'ten değil).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.slow_query_selection import (
    DBACE_MARKER_LABEL,
    classify_system_query,
    query_fingerprint,
    select_slow_queries,
)
from tests.live_pg import LIVE_DSNS, SKIP_REASON, dsn_id, with_database

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

META_DATABASE = "dbace_meta_it"


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(params=LIVE_DSNS[:1], ids=dsn_id)
def dsn(request):
    return request.param


@pytest.fixture
async def meta(dsn):
    """dbace'in KENDİ veritabanı PostgreSQL olarak: migration'larla kurulan temiz bir şema."""
    import os

    from app.migrations_runner import apply_migrations
    from pathlib import Path

    admin = await asyncpg.connect(with_database(dsn, "postgres"))
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {META_DATABASE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {META_DATABASE}")
    finally:
        await admin.close()
    url = with_database(dsn, META_DATABASE)
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        await apply_migrations(conn, Path(__file__).resolve().parents[2] / "supabase" / "migrations")
    finally:
        await conn.close()

    # Uygulamanın motoru süreç başına tek: ölçüm için ayrı bir motor/oturum açılıyor.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield url, session_factory
    await engine.dispose()
    if os.environ.get("DBACE_KEEP_META_IT") != "1":
        admin = await asyncpg.connect(with_database(dsn, "postgres"))
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {META_DATABASE} WITH (FORCE)")
        finally:
            await admin.close()


# --- Referans uygulama (Commit 9 öncesi Python yolu) ------------------------------------------------


def reference_selection(rows: list[dict], *, sort: str, limit: int, include_system: bool,
                        min_total_ms: float, min_calls: int) -> dict:
    """Commit 9 ÖNCESİNDEKİ Python mantığı — SQL'in ona eşit olduğunu kanıtlamak için."""
    rows = sorted(rows, key=lambda r: (r["collected_at"], r["id"]))
    cycles = {r["collected_at"] for r in rows}
    mode = "delta" if len(cycles) > 1 else "snapshot"
    text_to_key: dict[str, str] = {}
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        suffix = (":dbace" if row["from_monitoring_role"] else "") + (":nested" if row["toplevel"] is False else "")
        text_key = query_fingerprint(row["query"]) + suffix
        key = text_to_key.setdefault(text_key, f"id:{row['queryid']}{suffix}" if row["queryid"] else text_key)
        grouped.setdefault(key, []).append(row)

    entries, filtered_system, filtered_insignificant = [], 0, 0
    for key, group in grouped.items():
        first, last = group[0], group[-1]
        if mode == "snapshot" or len(group) == 1:
            total, calls = float(last["total_time_ms"] or 0), int(last["calls"] or 0)
        else:
            reset = last["total_time_ms"] < first["total_time_ms"] or last["calls"] < first["calls"]
            total = float(last["total_time_ms"] if reset else last["total_time_ms"] - first["total_time_ms"])
            calls = int(last["calls"] if reset else last["calls"] - first["calls"])
        if total <= 0:
            continue
        total = round(total, 2)
        reason = classify_system_query(last["query"])
        marker_conflict = reason == DBACE_MARKER_LABEL and last["from_monitoring_role"] is False
        if marker_conflict:
            reason = None
        entry = {
            "key": key, "queryid": last["queryid"], "calls": calls, "total_time_ms": total,
            "mean_time_ms": round(total / calls, 2) if calls > 0 else float(last["mean_time_ms"] or 0),
            "system_reason": reason, "sample_count": len(group), "marker_conflict": marker_conflict,
        }
        if reason is not None and not include_system:
            filtered_system += 1
            continue
        if entry["total_time_ms"] < min_total_ms or entry["calls"] < min_calls:
            filtered_insignificant += 1
            continue
        entries.append(entry)
    entries.sort(key=lambda e: {"total": e["total_time_ms"], "mean": e["mean_time_ms"], "calls": e["calls"]}[sort],
                 reverse=True)
    return {"entries": entries[:limit], "mode": mode, "filtered_system": filtered_system,
            "filtered_insignificant": filtered_insignificant, "visible": len(entries)}


async def _seed(url: str) -> tuple[int, list[dict]]:
    """Gerçekçi bir pencere: uygulama sorguları, dbace imzalı satırlar, iç içe çalıştırma, queryid'siz
    satırlar, sayaç sıfırlanması, eşik altı ve sistem sorguları."""
    conn = await asyncpg.connect(url)
    try:
        instance_id = await conn.fetchval(
            "INSERT INTO instances (name, engine, host, port, database, username, password, enabled) "
            "VALUES ($1, 'postgresql', 'h', 5432, 'd', 'u', 'p', true) RETURNING id", f"egress-{uuid.uuid4().hex[:8]}")
        now = datetime.now(UTC).replace(microsecond=0)
        texts = {
            "app": "SELECT id, total FROM orders WHERE status = $1 AND created_at > $2",
            "app2": "select   ID, Total\n from ORDERS where status = $1",  # aynı metnin biçimsiz hâli değil: ayrı sorgu
            "catalog": "SELECT count(*) FROM pg_catalog.pg_class",
            "marker": "/* dbace */ SELECT 1 FROM pg_stat_database",
            "small": "SELECT 1 FROM tiny",
        }
        rows = []
        for cycle in range(6):
            collected = now - timedelta(minutes=5 * (5 - cycle))
            for name, text in texts.items():
                queryid = None if name == "app2" else str(abs(hash(name)) % 10**9)
                from_monitoring_role = name == "marker"
                toplevel = False if name == "small" else True
                # "app" sorgusunda 4. turda sayaç sıfırlanıyor (pg_stat_statements_reset).
                base = 1000 * (cycle + 1)
                if name == "app" and cycle >= 4:
                    base = 500 * (cycle - 3)
                calls = base
                total = base * (2.5 if name != "small" else 0.01)
                rows.append({
                    "instance_id": instance_id, "collected_at": collected, "queryid": queryid, "query": text,
                    "from_monitoring_role": from_monitoring_role, "toplevel": toplevel, "calls": calls,
                    "total_time_ms": total, "mean_time_ms": total / max(calls, 1), "rows": calls,
                })
        await conn.executemany(
            "INSERT INTO slow_query_samples (instance_id, collected_at, queryid, query, from_monitoring_role, "
            "toplevel, calls, total_time_ms, mean_time_ms, rows, query_hash, query_class) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            [(r["instance_id"], r["collected_at"], r["queryid"], r["query"], r["from_monitoring_role"], r["toplevel"],
              r["calls"], r["total_time_ms"], r["mean_time_ms"], r["rows"],
              query_fingerprint(r["query"]), classify_system_query(r["query"]) or "") for r in rows])
        for index, row in enumerate(rows):
            row["id"] = index + 1
        return instance_id, rows
    finally:
        await conn.close()


@pytest.mark.parametrize(("sort", "include_system", "min_total_ms", "min_calls"), [
    ("total", False, 100.0, 1),
    ("mean", False, 100.0, 1),
    ("calls", True, 0.0, 0),
    ("total", True, 5000.0, 2000),
])
async def test_sql_selection_matches_the_reference_python_implementation(meta, sort, include_system, min_total_ms, min_calls):
    url, session_factory = meta
    instance_id, rows = await _seed(url)
    window = (datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) + timedelta(minutes=1))
    expected = reference_selection(rows, sort=sort, limit=20, include_system=include_system,
                                   min_total_ms=min_total_ms, min_calls=min_calls)
    async with session_factory() as session:
        selection = await select_slow_queries(
            session, instance_id, start=window[0], end=window[1], sort=sort, limit=20,
            include_system=include_system, min_total_ms=min_total_ms, min_calls=min_calls)
    got = [
        {"key": e.key, "queryid": e.queryid, "calls": e.calls, "total_time_ms": e.total_time_ms,
         "mean_time_ms": e.mean_time_ms, "system_reason": e.system_reason, "sample_count": e.sample_count,
         "marker_conflict": e.marker_conflict}
        for e in selection.entries
    ]
    log(f"sıralama={sort} sistem={include_system} eşik={min_total_ms}/{min_calls}",
        {"SQL": [(e["key"][:18], e["calls"], e["total_time_ms"]) for e in got],
         "referans": [(e["key"][:18], e["calls"], e["total_time_ms"]) for e in expected["entries"]],
         "gizlenen": (selection.filtered_system, selection.filtered_insignificant)})
    assert got == expected["entries"]
    assert selection.mode == expected["mode"]
    assert selection.filtered_system == expected["filtered_system"]
    assert selection.filtered_insignificant == expected["filtered_insignificant"]
    assert selection.visible_count == expected["visible"]


async def test_list_and_count_read_few_rows_and_text_only_for_the_page(meta):
    """Ölçüm: pencerede 30 satır varken liste/sayım yolu onlarcasını değil, sayfa kadarını okuyor."""
    from app.services.slow_query_selection import count_slow_in_list_view

    url, session_factory = meta
    instance_id, rows = await _seed(url)
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("SELECT pg_stat_statements_reset()")
        async with session_factory() as session:
            selection = await select_slow_queries(session, instance_id, start=datetime.now(UTC) - timedelta(hours=1),
                                                  end=datetime.now(UTC), sort="total", limit=3,
                                                  include_system=True, min_total_ms=0.0, min_calls=0)
            count, no_text = await count_slow_in_list_view(session, instance_id, threshold_ms=0.0, limit=20)
        stats = await conn.fetch(
            "SELECT query, calls, rows FROM pg_stat_statements WHERE query ILIKE '%slow_query_samples%' "
            "AND query NOT ILIKE '%pg_stat_statements%' ORDER BY rows DESC")
    finally:
        await conn.close()
    read_rows = sum(r["rows"] for r in stats)
    # `query_hash` / `query_class` metin DEĞİL: yalnızca metin kolonunu okuyan ifadeler sayılıyor.
    text_column = re.compile(r"slow_query_samples\.query(?![_a-zA-Z])")
    text_reads = [r for r in stats if text_column.search(" ".join(r["query"].split()))]
    log("okunan satır", {"penceredeki satır": len(rows), "sorgunun döndürdüğü toplam satır": read_rows,
                         "metin okuyan sorgu": [(r["rows"], " ".join(r["query"].split())[:80]) for r in text_reads]})
    assert len(selection.entries) == 3 and count >= 1
    assert all(e.query for e in selection.entries), "sayfadaki kalemlerin metni okunmalı"
    assert all(e.query == "" for e in no_text.entries), "sayım yolu metin okumamalı"
    assert read_rows <= len(rows) // 2, f"pencerenin yarısından az satır okunmalıydı: {read_rows}/{len(rows)}"
    assert 0 < sum(r["rows"] for r in text_reads) <= 3, "metin yalnızca sayfadaki kalemler için okunmalı"


async def test_outage_gaps_are_detected_identically_in_sql_and_python(meta):
    """Kesinti boşluğu PostgreSQL'de SQL'de (`LAG`), SQLite'ta Python'da — aynı sonucu vermeli."""
    from app.services import availability
    from app.models import Instance

    url, session_factory = meta
    conn = await asyncpg.connect(url)
    try:
        instance_id = await conn.fetchval(
            "INSERT INTO instances (name, engine, host, port, database, username, password, enabled) "
            "VALUES ($1, 'postgresql', 'h', 5432, 'd', 'u', 'p', true) RETURNING id", f"gap-{uuid.uuid4().hex[:8]}")
        now = datetime.now(UTC).replace(microsecond=0)
        # 15 saniyelik düzenli örnekler, ortada 40 dakikalık ve sonda 20 dakikalık boşluk.
        stamps = [now - timedelta(hours=3) + timedelta(seconds=15 * i) for i in range(40)]
        stamps += [s + timedelta(minutes=40) for s in stamps[-20:]]
        await conn.executemany("INSERT INTO metric_samples (instance_id, collected_at) VALUES ($1, $2)",
                               [(instance_id, s) for s in stamps])
    finally:
        await conn.close()

    async with session_factory() as session:
        instance = await session.get(Instance, instance_id)
        period = (stamps[0], now)
        assert session.bind.dialect.name == "postgresql"
        sql_outages = await availability.outages_for_instance(session, instance, *period)
        in_period = [
            availability.MetricSample.instance_id == instance_id,
            availability.MetricSample.collected_at >= period[0],
            availability.MetricSample.collected_at <= period[1],
        ]
        threshold = availability.gap_threshold(instance)
        pairs_sql = await availability._gaps(session, in_period, threshold)
        # Python yolu: aynı çiftler, eşik Python'da uygulanıyor (SQLite dalı).
        dialect = session.bind.dialect.name
        try:
            session.bind.dialect.name = "sqlite"
            pairs_python = [p for p in await availability._gaps(session, in_period, threshold)
                            if (p[1] - p[0]).total_seconds() >= threshold]
        finally:
            session.bind.dialect.name = dialect
    log("boşluklar", {"SQL": [(a.isoformat()[11:19], b.isoformat()[11:19]) for a, b in pairs_sql],
                      "Python": [(a.isoformat()[11:19], b.isoformat()[11:19]) for a, b in pairs_python],
                      "kesinti": [(o["seconds"], o["ongoing"]) for o in sql_outages]})
    assert pairs_sql == pairs_python
    assert len(sql_outages) == 2 and sql_outages[-1]["ongoing"] is True


async def test_migration_fingerprint_matches_python(meta):
    """#53 var olan satırların parmak izini SQL'de hesaplıyor; Python'la aynı olmalı (yoksa aynı sorgu
    migration sınırında İKİ gruba bölünürdü)."""
    url, _ = meta
    corpus = [
        "SELECT 1",
        "select   1",
        "SELECT\t1\n  FROM   orders\r\n WHERE x = $1",
        "  SELECT ÜRÜN FROM sipariş WHERE ad = 'Ağrı'  ",
        "/* dbace */ SELECT pg_sleep($1)",
        "SELECT 'çok satırlı\nmetin' FROM t",
    ]
    conn = await asyncpg.connect(url)
    try:
        instance_id = await conn.fetchval(
            "INSERT INTO instances (name, engine, host, port, database, username, password, enabled) "
            "VALUES ($1, 'postgresql', 'h', 5432, 'd', 'u', 'p', true) RETURNING id", f"fp-{uuid.uuid4().hex[:8]}")
        await conn.executemany(
            "INSERT INTO slow_query_samples (instance_id, query, collected_at) VALUES ($1, $2, now())",
            [(instance_id, text) for text in corpus])
        # Migration'daki ifadenin AYNISI (supabase/migrations/20260918090000_slow_query_sample_identity.sql).
        rows = await conn.fetch(
            "SELECT query, 'q:' || substr(encode(sha256(convert_to("
            "lower(regexp_replace(btrim(query, E' \\t\\n\\r\\f\\v'), E'[ \\t\\n\\r\\f\\v]+', ' ', 'g')), 'UTF8')), "
            "'hex'), 1, 24) AS sql_hash FROM slow_query_samples WHERE instance_id = $1", instance_id)
    finally:
        await conn.close()
    mismatched = [(r["query"], r["sql_hash"], query_fingerprint(r["query"]))
                  for r in rows if r["sql_hash"] != query_fingerprint(r["query"])]
    log("parmak izi", {"metin": len(rows), "uyuşmayan": mismatched})
    assert mismatched == []


async def test_application_never_queries_pg_timezone_names(meta):
    """Canlıda 74 çağrı / 88 bin satır `pg_timezone_names` görülüyor. Uygulamanın kendi yolları bunu
    çağırmıyor — kaynağı dbace DEĞİL (Supabase Studio gibi bir istemci)."""
    url, session_factory = meta
    instance_id, _ = await _seed(url)
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("SELECT pg_stat_statements_reset()")
        async with session_factory() as session:
            await select_slow_queries(session, instance_id, start=datetime.now(UTC) - timedelta(hours=1),
                                      end=datetime.now(UTC), limit=5)
        timezone_calls = await conn.fetchval(
            "SELECT coalesce(sum(calls), 0) FROM pg_stat_statements WHERE query ILIKE '%pg_timezone_names%' "
            "AND query NOT ILIKE '%pg_stat_statements%'")
    finally:
        await conn.close()
    log("pg_timezone_names çağrısı (uygulama yolu)", timezone_calls)
    assert timezone_calls == 0
