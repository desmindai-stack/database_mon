"""Index önerisinin sorgu ayrıştırması (Faz 27 İŞ 1).

CANLI HATA: `'public.recurse' tablosu bu veritabanında bulunamadı`.

`recurse` bir CTE adıdır (`WITH RECURSIVE recurse AS (...)`), tablo değil. Elle yazılmış
regex `FROM recurse` görüp gerçek tablo sandı, katalogda aramaya gitti, bulamadı ve index
önerisi hiç üretilemedi. Kullanıcının bildirdiği "index önerisi bugüne kadar hiç üretilemedi"
durumunun sebebi buydu.

Bu dosya iki şeyi kanıtlıyor: (1) tablo çıkarımı artık doğru, (2) öneri UÇTAN UCA üretiliyor.
"""

from __future__ import annotations

import app.services.index_advisor as index_advisor_module
from app.collectors.base import ConnectionTarget
from app.services.index_advisor import PostgreSQLIndexAdvisor
from tests.fakes import FakeAsyncConnection

RECURSIVE_CTE_QUERY = """
WITH RECURSIVE recurse AS (
    SELECT 1 AS n
    UNION ALL
    SELECT n + 1 FROM recurse WHERE n < 5
)
SELECT pn.name, o.total
FROM person pn
JOIN orders o ON o.person_id = pn.id
CROSS JOIN recurse r
WHERE pn.city = 'Ankara' AND r.n > 2
"""
# `r.n > 2` koşulu bilerek var: CTE takma adına yazılmış bir filtre, eski regex'li sürümde
# `recurse` için aday kolon üretiyordu. Aday listesinden CTE adlarının atıldığını bu koşul
# olmadan doğrulayamazdık.

#: Katalog cevapları: tablo var, index yok, istatistik yok.
CATALOG = {
    "EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False,
    "FROM pg_tables WHERE schemaname": True,
    "FROM pg_class c": {"row_count": 50000, "total_bytes": 5_000_000},
    "FROM pg_indexes": [],
    "FROM pg_stats": None,
    "reltuples::bigint FROM pg_class c JOIN pg_namespace": 50000,
}


def _advisor() -> PostgreSQLIndexAdvisor:
    return PostgreSQLIndexAdvisor(
        ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    )


async def _patch(monkeypatch, conn: FakeAsyncConnection) -> None:
    async def fake_connect(**kwargs):
        return conn

    monkeypatch.setattr(index_advisor_module.asyncpg, "connect", fake_connect)


def test_the_cte_name_is_not_extracted_as_a_table():
    """Regex'in düştüğü tuzak: `FROM recurse` bir CTE başvurusudur."""
    tables = _advisor()._extract_tables(RECURSIVE_CTE_QUERY)
    names = {name for _alias, name, _schema in tables}
    assert "recurse" not in names
    assert names == {"person", "orders"}


def test_aliases_are_carried_so_columns_can_be_attributed():
    """`pn.city` filtresinin `person` tablosuna ait olduğunu anlamak takma adı gerektiriyor."""
    tables = _advisor()._extract_tables(RECURSIVE_CTE_QUERY)
    by_alias = {alias: name for alias, name, _ in tables}
    assert by_alias["pn"] == "person"
    assert by_alias["o"] == "orders"


async def test_advice_is_produced_end_to_end_for_a_query_with_a_cte(monkeypatch):
    """UÇTAN UCA: canlıda hiç öneri üretemeyen sorgu artık öneri üretiyor."""
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, reasons = await _advisor().advise(RECURSIVE_CTE_QUERY, calls=100)

    assert advice, f"öneri üretilemedi, sebepler: {[r.code for r in reasons]}"
    tables = {a.table_name for a in advice}
    assert "recurse" not in tables
    assert "person" in tables
    ddl = " ".join(a.index_ddl for a in advice)
    assert "city" in ddl


async def test_a_truncated_query_is_refused_with_a_fix_command(monkeypatch):
    """Kesik bir sorgudan çıkarılan tablo/kolon listesi EKSİKTİR; ona göre üretilen öneri
    yanlış olur ve kullanıcı onu canlıda uygular. "Öneri üretemedim" demek yeğdir."""
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, reasons = await _advisor().advise(
        "SELECT pn.name FROM person pn WHERE pn.id IN (SELECT id FROM ...", calls=100
    )
    assert advice == []
    codes = [r.code for r in reasons]
    assert codes == ["truncated_query"]
    assert "track_activity_query_size" in reasons[0].what_to_do


async def test_an_unparsable_query_says_so_instead_of_guessing(monkeypatch):
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT FROM WHERE ))) ORDER", calls=100)
    assert advice == []
    assert [r.code for r in reasons] == ["unparsable_query"]
    # "Tahmin yürütmek yerine öneri üretilmiyor" gerekçesi kullanıcıya söyleniyor.
    assert "yanlış bir tabloya" in reasons[0].what_to_do


async def test_a_query_touching_only_a_cte_reports_no_real_table(monkeypatch):
    """Gerçek tabloya hiç dokunmayan bir sorguda index önerilecek bir şey yoktur — ve bu
    bir hata değil, açıklanması gereken bir durum."""
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, reasons = await _advisor().advise(
        "WITH t AS (SELECT 1 AS n) SELECT n FROM t WHERE n = 1", calls=100
    )
    assert advice == []
    assert [r.code for r in reasons] == ["no_query_data"]
    assert "CTE" in reasons[0].what_to_do


async def test_a_function_source_is_not_advised_on(monkeypatch):
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, reasons = await _advisor().advise(
        "SELECT g FROM generate_series(1, 100) AS g WHERE g = 5", calls=100
    )
    assert advice == []
    assert [r.code for r in reasons] == ["no_query_data"]


async def test_schema_qualified_tables_keep_their_schema(monkeypatch):
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, _ = await _advisor().advise(
        "SELECT * FROM billing.invoices i WHERE i.state = 'open'", calls=100
    )
    assert advice
    assert advice[0].schema_name == "billing"


async def test_placeholders_do_not_prevent_advice(monkeypatch):
    """Normalize edilmiş metin ($1) index önerisini engellemez: hangi kolonun filtrelendiği
    yer tutucudan bağımsız olarak bellidir. EXPLAIN'den farklı bir durum."""
    conn = FakeAsyncConnection(dict(CATALOG))
    await _patch(monkeypatch, conn)
    advice, reasons = await _advisor().advise(
        "SELECT * FROM orders o WHERE o.customer_id = $1 AND o.status = $2", calls=100
    )
    assert advice, f"sebepler: {[r.code for r in reasons]}"
    assert advice[0].table_name == "orders"
