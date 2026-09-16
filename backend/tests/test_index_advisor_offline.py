"""Index önerisinin veritabanına BAĞLANMADAN verdiği kararlar (Faz 27 İŞ 1, Faz 31 İŞ 1).

Bu dosyada sahte bağlantı YOK. Buradaki her dal, danışman izlenen sunucuya bağlanmadan
önce karar veriyor; testler bunu da kanıtlıyor: bağlantı fonksiyonu çağrılırsa test
kırılıyor. Katalog gerektiren her şey (tablo var mı, index var mı, niteliksiz kolon hangi
tabloda, ifade IMMUTABLE mı) gerçek sunucuya karşı `test_index_advice_live_postgres.py`'de.

Faz 31'den önce bu dosya katalog cevaplarını `FakeAsyncConnection` ile taklit ediyordu. Bu
özellik sınıfında sahte bağlantılı testler üç tur boyunca yeşil kalıp canlıda çalışmayan bir
özelliği örtmüştü; katalog davranışı artık yalnızca gerçek sunucuda doğrulanıyor.
"""

from __future__ import annotations

import pytest

import app.services.index_advisor as index_advisor_module
from app.collectors.base import ConnectionTarget
from app.collectors.query_marker import DBACE_QUERY_MARKER
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.sql_analysis import analyze_query

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


@pytest.fixture
def advisor(monkeypatch) -> PostgreSQLIndexAdvisor:
    async def must_not_connect(**kwargs):
        raise AssertionError("bu dal veritabanına bağlanmamalıydı")

    monkeypatch.setattr(index_advisor_module, "connect_marked", must_not_connect)
    return PostgreSQLIndexAdvisor(
        ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    )


def test_the_cte_name_is_not_extracted_as_a_table():
    names = {t.name for t in analyze_query(RECURSIVE_CTE_QUERY).tables}
    assert names == {"person", "orders"}


def test_aliases_are_carried_so_columns_can_be_attributed():
    by_alias = {t.alias: t.name for t in analyze_query(RECURSIVE_CTE_QUERY).tables}
    assert by_alias["pn"] == "person"
    assert by_alias["o"] == "orders"


def test_unqualified_catalog_table_resolves_to_pg_catalog_not_public():
    """CANLI HATA (Faz 31): `'public.pg_class' tablosu bu veritabanında bulunamadı`."""
    table = analyze_query("SELECT relname FROM pg_class WHERE relkind = $1").tables[0]
    assert (table.schema, table.schema_assumed) == ("pg_catalog", False)


def test_explicit_schema_is_kept_and_default_is_marked_as_assumed():
    tables = {t.name: t for t in analyze_query("SELECT * FROM billing.invoices i JOIN orders o ON o.id = i.order_id").tables}
    assert (tables["invoices"].schema, tables["invoices"].schema_assumed) == ("billing", False)
    assert (tables["orders"].schema, tables["orders"].schema_assumed) == ("public", True)


async def test_empty_query_returns_empty_status(advisor):
    result = await advisor.advise("")
    assert result.status == "empty"
    assert result.recommendations == [] and result.reasons == []


async def test_a_truncated_query_is_refused_with_a_fix_command(advisor):
    result = await advisor.advise("SELECT pn.name FROM person pn WHERE pn.id IN (SELECT id FROM ...", calls=100)
    assert result.status == "truncated"
    assert [r.code for r in result.reasons] == ["truncated_query"]
    assert "track_activity_query_size" in result.reasons[0].what_to_do


async def test_an_unparsable_query_says_so_instead_of_guessing(advisor):
    result = await advisor.advise("SELECT FROM WHERE ))) ORDER", calls=100)
    assert result.status == "unparsable"
    assert [r.code for r in result.reasons] == ["unparsable_query"]
    assert "yanlış bir tabloya" in result.reasons[0].what_to_do


@pytest.mark.parametrize(
    "query",
    [
        "SELECT relname FROM pg_class WHERE relkind = $1",
        "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = $1",
        "SELECT table_name FROM information_schema.tables WHERE table_schema = $1",
        "SELECT pid, state FROM pg_stat_activity WHERE datname = $1",
        f"{DBACE_QUERY_MARKER} SELECT count(*) FROM orders WHERE status = $1",
    ],
    ids=["pg_class-semasiz", "pg_catalog-nitelikli", "information_schema", "pg_stat", "dbace-imzasi"],
)
async def test_system_queries_never_enter_index_analysis(advisor, query):
    result = await advisor.advise(query, calls=10_000)
    assert result.status == "system"
    assert [r.code for r in result.reasons] == ["system_query"]
    assert result.predicates == []


async def test_structural_check_catches_catalog_query_the_patterns_miss(advisor, monkeypatch):
    """Desen listesi boşaltılsa bile tüm tabloları pg_catalog'da olan sorgu sistem sorgusu."""
    monkeypatch.setattr(index_advisor_module, "classify_system_query", lambda q: None)
    result = await advisor.advise("SELECT oid FROM pg_catalog.pg_am WHERE amname = $1", calls=100)
    assert result.status == "system"


async def test_a_query_touching_only_a_cte_reports_no_real_table(advisor):
    result = await advisor.advise("WITH t AS (SELECT 1 AS n) SELECT n FROM t WHERE n = 1", calls=100)
    assert [r.code for r in result.reasons] == ["no_query_data"]


async def test_a_function_source_is_not_advised_on(advisor):
    result = await advisor.advise("SELECT g FROM generate_series(1, 100) AS g WHERE g = 5", calls=100)
    assert [r.code for r in result.reasons] == ["no_query_data"]


async def test_below_threshold_returns_counts_and_predicates_without_connecting(advisor):
    """FAZ 31 İŞ 1c: eşik altındaki sorgu için izlenen sunucuya HİÇ bağlanılmıyor; yine de
    bulunan filtreler gösteriliyor."""
    result = await advisor.advise("SELECT * FROM orders WHERE status = $1", calls=2, min_calls=5)
    assert result.status == "below_threshold"
    assert (result.calls_now, result.threshold) == (2, 5)
    reason = result.reasons[0]
    assert reason.code == "insufficient_samples"
    assert "2/5" in reason.message
    assert "tekrar deneyin" not in reason.message.lower()
    assert [(p.column, p.kind) for p in result.predicates] == [("status", "eq")]


async def test_threshold_is_the_configured_value_not_a_constant(advisor):
    result = await advisor.advise("SELECT * FROM orders WHERE status = $1", calls=40, min_calls=50)
    assert result.status == "below_threshold"
    assert "40/50" in result.reasons[0].message
