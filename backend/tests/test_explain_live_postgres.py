"""EXPLAIN ve index önerisinin GERÇEK PostgreSQL'e karşı doğrulanması (Faz 29 İŞ 1).

## Neden bu dosya var

EXPLAIN özelliği üç tur boyunca "düzeltildi" ama canlıda hiç çalışmadı. Her turda testler
yeşildi — çünkü hepsi **sahte bağlantı** kullanıyordu ve sahte bağlantı, asıl kırılan şeyi
taklit edemiyordu: sorgunun sunucuya HANGİ PROTOKOLLE gittiğini.

Son hata da tam oradaydı:

    the server expects 4 arguments for this query, 0 were passed

Bu hatayı sunucu değil **asyncpg** üretiyor ve sahte bir bağlantıda hiç oluşmuyor. Yani
sahte bağlantı testi, bu özellik için "çalışıyor" diyemez — sadece "çağrıldı" diyebilir.

## Nasıl çalışıyor

`DBACE_TEST_PG_DSN` tanımlıysa ona bağlanıyor; tanımlı değilse tüm dosya atlanıyor
(CI'da PostgreSQL yok). Yerelde çalıştırmak için:

    docker run -d --name dbace-pg17 -e POSTGRES_PASSWORD=dbace -e POSTGRES_DB=dbace \\
        -p 55432:5432 postgres:17
    set DBACE_TEST_PG_DSN=postgresql://postgres:dbace@127.0.0.1:55432/dbace
    pytest tests/test_explain_live_postgres.py -v

Birden çok sürümü aynı anda denemek için `DBACE_TEST_PG_DSN` virgülle ayrılmış liste kabul
ediyor. Geliştirme sırasında PostgreSQL 17.11 ve 15.19 ile koşuldu; ikisinde de geçti.

## Bu testlerin KANITLADIĞI şey

1. Yer tutuculu (normalize) bir sorgu için plan gerçekten alınabiliyor.
2. Parametre değerleri plana KARIŞMIYOR (filtrelerde `$N` korunuyor) ve plan çökmüyor —
   yani "bir şey döndü" değil, değerden bağımsız DOĞRU bir plan döndü.
3. Elenen "NULL bağla" çözümünün planı çökerttiği, kalıcı bir testle sabit.

   Planın psql'in `EXPLAIN (GENERIC_PLAN)` çıktısıyla düğüm düğüm aynı olduğu geliştirme
   sırasında elle doğrulandı (çıktı ILERLEME.md'de). Bu karşılaştırma teste ALINMADI:
   referansı almanın tek yolu psql, çünkü asyncpg o komutu hiç gönderemiyor — testin
   kendisi de aynı kısıta tabi. İddiayı kanıtlayamayan bir test yazmaktansa, testin
   kanıtlayabildiği şeyi (değerden bağımsızlık) kanıtlaması tercih edildi.
4. GENERIC_PLAN'i desteklemeyen sürümde (PG 15) de çalışıyor.
5. Index önerisinin hypopg fayda ölçümü normalize sorguda çalışıyor.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.collectors.base import ConnectionTarget
from app.services.explain_service import PostgreSQLExplainService
from app.services.generic_plan import explain_json
from app.services.index_advisor import PostgreSQLIndexAdvisor

asyncpg = pytest.importorskip("asyncpg")

_DSNS = [d.strip() for d in os.environ.get("DBACE_TEST_PG_DSN", "").split(",") if d.strip()]

pytestmark = pytest.mark.skipif(
    not _DSNS,
    reason=(
        "Gerçek PostgreSQL yok. DBACE_TEST_PG_DSN tanımlayın "
        "(ör. postgresql://postgres:dbace@127.0.0.1:55432/dbace)."
    ),
)

JOIN_QUERY = (
    "SELECT o.id, o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE o.status = $1 AND o.total > $2 AND c.segment = $3 AND o.created_at > $4 "
    "ORDER BY o.created_at DESC LIMIT $5"
)
CTE_QUERY = (
    "WITH recent AS (SELECT * FROM orders WHERE created_at > $1) "
    "SELECT status, count(*) FROM recent WHERE total > $2 GROUP BY status"
)
PLAIN_QUERY = "SELECT count(*) FROM orders WHERE status = 'paid'"


def _target(dsn: str) -> ConnectionTarget:
    url = urlparse(dsn)
    return ConnectionTarget(
        host=url.hostname or "127.0.0.1",
        port=url.port or 5432,
        database=(url.path or "/postgres").lstrip("/"),
        username=url.username or "postgres",
        password=url.password or "",
    )


@pytest.fixture(params=_DSNS, ids=lambda d: d.rsplit("@", 1)[-1])
def dsn(request):
    return request.param


@pytest.fixture
async def conn(dsn):
    connection = await asyncpg.connect(dsn, statement_cache_size=0)
    # Test verisi: her koşuda yeniden kurulabilir olmalı, çünkü konteyner taze olabilir.
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id bigserial PRIMARY KEY, name text NOT NULL, segment text NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
            id bigserial PRIMARY KEY, customer_id bigint NOT NULL, status text NOT NULL,
            total numeric NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
        """
    )
    count = await connection.fetchval("SELECT count(*) FROM orders")
    if not count:
        await connection.execute(
            "INSERT INTO customers (name, segment) SELECT 'c'||g, "
            "CASE WHEN g %% 3 = 0 THEN 'gold' ELSE 'std' END FROM generate_series(1, 2000) g"
        )
        await connection.execute(
            "INSERT INTO orders (customer_id, status, total, created_at) "
            "SELECT (g %% 2000) + 1, CASE WHEN g %% 5 = 0 THEN 'paid' ELSE 'new' END, "
            "(g %% 900)::numeric, now() - (g || ' minutes')::interval "
            "FROM generate_series(1, 50000) g"
        )
        await connection.execute("ANALYZE")
    try:
        yield connection
    finally:
        await connection.close()


def _shape(plan) -> list[tuple]:
    """Planın karşılaştırılabilir iskeleti: düğüm tipi, tablo, filtre, satır tahmini."""
    root = plan[0]["Plan"] if isinstance(plan, list) else plan["Plan"]
    rows: list[tuple] = []

    def walk(node, depth=0):
        rows.append(
            (
                depth,
                node.get("Node Type"),
                node.get("Relation Name"),
                node.get("Filter"),
                node.get("One-Time Filter"),
                round(float(node.get("Plan Rows") or 0)),
            )
        )
        for child in node.get("Plans") or []:
            walk(child, depth + 1)

    walk(root)
    return rows


# --- Kök nedenin kendisi -------------------------------------------------------------------


async def test_direct_explain_on_a_normalized_query_is_impossible_through_asyncpg(conn):
    """ÜÇ TURDUR SÜREN HATANIN KENDİSİ.

    Bu test geçtiği sürece, "doğrudan EXPLAIN gönderelim" çözümüne geri dönmek imkânsız:
    asyncpg satır döndüren sorguları genişletilmiş protokolle yolluyor, sunucu `$N`
    yer tutucularını parametre sanıyor ve istek daha gönderilmeden düşüyor.
    """
    with pytest.raises(Exception) as excinfo:
        await conn.fetchval(f"EXPLAIN (FORMAT JSON) {JOIN_QUERY}")
    assert "expects 5 arguments" in str(excinfo.value)


async def test_binding_nulls_would_produce_a_meaningless_plan(conn):
    """ELENEN ÇÖZÜM: parametre sayısı kadar NULL bağlamak.

    Çalışıyor ama planı çöpe çeviriyor — planlayıcı NULL'lardan "bu sorgu hiçbir şey
    döndürmez" sonucunu çıkarıyor. Bu test, o yolun neden seçilmediğinin kalıcı kanıtı.
    """
    version = await conn.fetchval("SELECT current_setting('server_version_num')::int")
    if version < 160_000:
        pytest.skip("GENERIC_PLAN seçeneği PostgreSQL 16 ile geldi; bu yol burada zaten yok")
    raw = await conn.fetchval(
        f"EXPLAIN (GENERIC_PLAN, FORMAT JSON) {JOIN_QUERY}", *([None] * 5)
    )
    plan = json.loads(raw) if isinstance(raw, str) else raw
    assert any(row[4] == "false" for row in _shape(plan)), (
        "NULL bağlamak planı çökertmeliydi; çökertmiyorsa bu testin dayanağı değişmiş demektir"
    )


# --- Seçilen yolun doğruluğu ---------------------------------------------------------------


async def test_parameter_values_never_leak_into_the_plan(conn):
    """Değerden bağımsızlığın kanıtı: filtrelerde `$N` KORUNUYOR.

    Filtrede gerçek bir değer görünseydi (ör. `status = 'paid'`), plan o değere özgü
    olurdu ve "generic" iddiası yalan olurdu.
    """
    plan = await explain_json(conn, JOIN_QUERY)
    filters = [row[3] for row in _shape(plan) if row[3]]
    assert filters, "filtre bulunamadı — test verisi beklenenden farklı"
    joined = " ".join(filters)
    assert "$" in joined, f"yer tutucular plandan silinmiş: {joined}"
    # Plan çökmemiş olmalı.
    assert not any(row[4] == "false" for row in _shape(plan))


async def test_cte_query_with_placeholders_is_planned(conn):
    plan = await explain_json(conn, CTE_QUERY)
    assert _shape(plan)
    assert not any(row[4] == "false" for row in _shape(plan))


async def test_a_query_without_placeholders_still_works(conn):
    plan = await explain_json(conn, PLAIN_QUERY)
    assert _shape(plan)[0][1] in ("Aggregate", "Finalize Aggregate")


async def test_no_prepared_statement_is_leaked_even_when_explain_fails(conn):
    """Hata yolunda temizlik. Sızan hazırlanmış ifadeler, uzun ömürlü bir bağlantıda
    birikir ve sunucu belleğini yer."""
    with pytest.raises(Exception):
        await explain_json(conn, "SELECT * FROM boyle_bir_tablo_yok WHERE a = $1")
    leaked = await conn.fetchval(
        "SELECT count(*) FROM pg_prepared_statements WHERE name LIKE 'dbace_plan_%'"
    )
    assert leaked == 0


async def test_two_plans_on_the_same_connection_do_not_collide(conn):
    """Index önerisi aynı bağlantıda birden çok sorgu planlıyor; sabit bir ifade adı
    "prepared statement already exists" hatası verirdi."""
    first = await explain_json(conn, JOIN_QUERY)
    second = await explain_json(conn, CTE_QUERY)
    assert _shape(first) and _shape(second)


# --- Servis katmanı: uçtan uca -------------------------------------------------------------


async def test_explain_service_returns_a_plan_for_a_normalized_query(dsn):
    """Sahte bağlantıyla değil, kullanıcının tıkladığında çalışan kodla."""
    service = PostgreSQLExplainService(_target(dsn))
    result = await service.explain(JOIN_QUERY)
    assert result.plan is not None
    assert result.total_cost and result.total_cost > 0
    # Sınır kullanıcıya söyleniyor.
    assert result.caveat and "DEĞERDEN BAĞIMSIZ" in result.caveat


async def test_explain_service_refuses_analyze_on_a_normalized_query(dsn):
    """EXPLAIN ANALYZE sorguyu GERÇEKTEN çalıştırır; uydurma değerlerle çalıştırmak izlenen
    veritabanında öngörülemez maliyet çıkarır."""
    service = PostgreSQLExplainService(_target(dsn))
    with pytest.raises(ValueError) as excinfo:
        await service.explain(JOIN_QUERY, analyze=True)
    assert "GERÇEKTEN çalıştırdığı" in str(excinfo.value)


async def test_explain_service_refuses_truncated_text_without_asking_the_server(dsn):
    service = PostgreSQLExplainService(_target(dsn))
    with pytest.raises(ValueError) as excinfo:
        await service.explain(
            "SELECT o.id, pn.name FROM orders o JOIN product_names pn ON pn.id = o.id "
            "WHERE o.stat..."
        )
    assert "kesilmiş" in str(excinfo.value).lower() or "eksik" in str(excinfo.value).lower()


async def test_index_advisor_measures_benefit_for_a_normalized_query(conn, dsn):
    """Index önerisinin hypopg ölçümü, yer tutuculu sorgularda BİLE ÇALIŞMIYORDU: kod
    `not _has_placeholders(...)` koşuluyla onu atlıyordu ve pg_stat_statements'tan gelen
    her sorgu yer tutuculu olduğu için ölçüm hiç yapılmıyordu."""
    has_hypopg = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')"
    )
    if not has_hypopg:
        pytest.skip("hypopg kurulu değil; fayda ölçümü doğrulanamıyor")

    advisor = PostgreSQLIndexAdvisor(_target(dsn))
    result = await advisor.advise(
        "SELECT o.id, o.total FROM orders o WHERE o.status = $1 AND o.created_at > $2 "
        "ORDER BY o.created_at DESC LIMIT $3",
        calls=1000,
    )
    advices = result.recommendations
    assert advices, f"öneri üretilmedi: {[r.message for r in result.reasons]}"
    measured = [a for a in advices if a.has_hypopg_estimate]
    assert measured, "hypopg ölçümü yapılmadı — yer tutucu şartı geri gelmiş olabilir"
    best = measured[0]
    assert best.before_cost and best.after_cost
    assert best.after_cost < best.before_cost


# --- pg_stat_statements tam sömürüsü (Faz 29 İŞ 2a) ----------------------------------------


async def test_slow_query_collection_works_on_this_server_version(dsn):
    """Toplayıcının pg_stat_statements sorgusu BU sürümde çalışıyor mu.

    Sürüm farkı burada teorik değil: PostgreSQL 17, `blk_read_time` sütununu
    `shared_blk_read_time` olarak YENİDEN ADLANDIRDI. Eski adı 17'ye göndermek
    "column does not exist" ile TÜM yavaş sorgu toplamasını düşürürdü — tek bir sütun
    yüzünden özelliğin tamamı. Sahte bağlantı bunu yakalayamaz.
    """
    from app.collectors.postgresql import PostgreSQLCollector

    collector = PostgreSQLCollector(_target(dsn))
    rows = await collector.collect_slow_queries(limit=5)
    assert rows, "pg_stat_statements boş — eklenti kurulu mu?"

    required = {
        "calls", "total_time_ms", "mean_time_ms", "stddev_time_ms", "min_time_ms",
        "max_time_ms", "blk_read_time_ms", "blk_write_time_ms", "shared_blks_dirtied",
        "shared_blks_written", "wal_records", "wal_fpi", "wal_bytes", "plans",
        "total_plan_time_ms", "jit_time_ms", "jit_functions",
        "temp_blk_read_time_ms", "temp_blk_write_time_ms",
    }
    missing = required - set(rows[0])
    assert not missing, f"toplayıcı şu alanları döndürmedi: {sorted(missing)}"


async def test_derived_metrics_survive_a_real_row(dsn):
    """Gerçek bir satırda türetme çöküyor mu — sıfıra bölme, None, tip uyuşmazlığı."""
    from app.collectors.postgresql import PostgreSQLCollector
    from app.domain.query_metrics import derive_metrics, flag_metrics

    collector = PostgreSQLCollector(_target(dsn))
    rows = await collector.collect_slow_queries(limit=5)
    total = sum(float(r.get("total_time_ms") or 0) for r in rows) or None

    for raw in rows:
        row = SimpleNamespace(**raw)
        derived = derive_metrics(row, total_time_all_ms=total)
        # Paydası olan her oran ya sayı ya None; asla NaN/sonsuz olmamalı.
        for key, value in derived.items():
            if isinstance(value, float):
                assert value == value, f"{key} NaN"
                assert value not in (float("inf"), float("-inf")), f"{key} sonsuz"
        flag_metrics(derived)  # patlamamalı


async def test_io_timing_distinguishes_off_from_no_io(conn, dsn):
    """`track_io_timing` kapalıyken I/O payı 0 DEĞİL None olmalı.

    Bu ayrım olmadan, ölçümü kapalı her sunucuda her sorgu "I/O beklemesi yok" görünür ve
    depolamaya bağlı bir darboğaz sistematik olarak gözden kaçardı.
    """
    from app.collectors.postgresql import PostgreSQLCollector
    from app.domain.query_metrics import derive_metrics

    setting = await conn.fetchval("SHOW track_io_timing")
    collector = PostgreSQLCollector(_target(dsn))
    rows = await collector.collect_slow_queries(limit=20)
    # Diskten gerçekten blok okumuş bir satır arıyoruz; yoksa ayrım test edilemez.
    candidate = next(
        (r for r in rows if float(r.get("shared_blks_read") or 0) > 0), None
    )
    if candidate is None:
        pytest.skip("Bu sunucuda diskten blok okuyan bir sorgu yok; ayrım test edilemiyor")

    derived = derive_metrics(SimpleNamespace(**candidate))
    if str(setting).lower() in ("on", "true"):
        assert derived["io_time_measured"] is True
    else:
        assert derived["io_time_measured"] is False
        assert derived["io_time_share_pct"] is None
