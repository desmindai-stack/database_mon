"""SQL Server sorgu metrikleri ve index önerileri (Faz 29 İŞ 2c).

Bu dosyanın koruduğu asıl fikir: **CPU süresi toplam süre DEĞİLDİR.**

Önceki kod `total_time_ms` alanına `total_worker_time` (CPU) yazıyordu. `total_elapsed_time`
ise duvar saatidir ve ikisinin FARKI BEKLEMEDİR. Yani "yavaş sorgu" listesi aslında "CPU
yiyen sorgu" listesiydi: kilitte 10 saniye bekleyip 5 ms CPU kullanan bir sorgu listede
HIZLI görünüyordu — oysa kullanıcının şikâyet ettiği tam olarak odur.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.collectors.sqlserver_mongodb import (
    _QS_BASE_COLUMNS,
    _QS_OPTIONAL_COLUMNS,
    build_slow_query_sql,
)
from app.domain.query_metrics import (
    MEMORY_GRANT_WASTE_WARN_PCT,
    WAIT_TIME_SHARE_WARN_PCT,
    derive_metrics,
    flag_metrics,
)
from app.services.mssql_index_advice import (
    advice_for_missing_index,
    advice_for_unused_index,
    index_ddl,
)

_ALL_COLUMNS = _QS_BASE_COLUMNS | {column for _, column, _ in _QS_OPTIONAL_COLUMNS}


# --- Sorgu kurucu --------------------------------------------------------------------------


def test_total_time_is_elapsed_not_cpu():
    """ÜÇ SATIRLIK AMA EN ÖNEMLİ TEST: liste beklemeyi görmeli.

    `total_worker_time` sıralaması, kilitte bekleyen sorguları listenin tamamen dışında
    bırakıyordu.
    """
    sql = build_slow_query_sql(_ALL_COLUMNS)
    assert "qs.total_elapsed_time / 1000.0 AS total_time_ms" in sql
    assert "qs.total_worker_time / 1000.0 AS cpu_time_ms" in sql
    assert "ORDER BY qs.total_elapsed_time DESC" in sql


def test_missing_optional_columns_become_null_not_errors():
    """SQL Server sütunları SP/CU ile ekliyor. Olmayan bir sütunu istemek "invalid column
    name" ile TÜM yavaş sorgu toplamasını düşürürdü; alan hep var, değeri NULL."""
    sql = build_slow_query_sql(_QS_BASE_COLUMNS)
    assert "NULL AS total_rows" not in sql  # alan adı dbace tarafında "rows"
    assert "NULL AS rows" in sql
    assert "NULL AS spills" in sql
    assert "NULL AS grant_kb" in sql
    # Temel sütunlar her zaman gerçek ifadeyle geliyor.
    assert "qs.total_logical_reads AS logical_reads" in sql


def test_present_optional_columns_are_used():
    sql = build_slow_query_sql(_QS_BASE_COLUMNS | {"total_spills", "total_rows"})
    assert "qs.total_spills AS spills" in sql
    assert "qs.total_rows AS rows" in sql
    assert "NULL AS grant_kb" in sql


def test_every_optional_column_produces_a_field_either_way():
    """Bir sütun unutulursa tüketici tarafında sessizce eksik alan olurdu."""
    with_all = build_slow_query_sql(_ALL_COLUMNS)
    with_none = build_slow_query_sql(_QS_BASE_COLUMNS)
    for field, _column, _expr in _QS_OPTIONAL_COLUMNS:
        assert f" AS {field}" in with_all, field
        assert f" AS {field}" in with_none, field


# --- CPU / bekleme ayrımı ------------------------------------------------------------------


def _row(**over):
    base = dict(
        calls=10,
        total_time_ms=1000.0,
        mean_time_ms=100.0,
        rows=100,
        cpu_time_ms=None,
        grant_kb=None,
        used_grant_kb=None,
        spills=None,
        shared_blks_hit=0,
        shared_blks_read=0,
        blk_read_time_ms=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_wait_share_is_the_difference_between_elapsed_and_cpu():
    derived = derive_metrics(_row(total_time_ms=1000.0, cpu_time_ms=250.0))
    assert derived["cpu_time_share_pct"] == pytest.approx(25.0)
    assert derived["wait_time_share_pct"] == pytest.approx(75.0)


def test_a_waiting_query_is_flagged():
    """Sorguyu optimize etmenin boşa emek olduğu durum."""
    derived = derive_metrics(_row(total_time_ms=1000.0, cpu_time_ms=100.0))
    assert derived["wait_time_share_pct"] > WAIT_TIME_SHARE_WARN_PCT
    assert "wait_time_share_pct" in {f["key"] for f in flag_metrics(derived)}


def test_a_cpu_bound_query_is_not_flagged_as_waiting():
    derived = derive_metrics(_row(total_time_ms=1000.0, cpu_time_ms=950.0))
    assert "wait_time_share_pct" not in {f["key"] for f in flag_metrics(derived)}


def test_cpu_share_is_none_when_cpu_time_was_not_measured():
    """PostgreSQL'de pg_stat_kcache yoksa CPU süresi yok. 0 göstermek "hiç CPU kullanmadı"
    demek olurdu ve sorgu %100 bekliyor gibi görünürdü."""
    derived = derive_metrics(_row(cpu_time_ms=None))
    assert derived["cpu_time_share_pct"] is None
    assert derived["wait_time_share_pct"] is None


def test_wait_share_never_goes_negative():
    """CPU süresi paralel planlarda elapsed'i AŞABİLİR (birden çok çekirdek). Negatif bekleme
    anlamsız olurdu."""
    derived = derive_metrics(_row(total_time_ms=100.0, cpu_time_ms=400.0))
    assert derived["wait_time_share_pct"] == 0.0


def test_memory_grant_waste_is_flagged():
    derived = derive_metrics(_row(grant_kb=10_000, used_grant_kb=1_000))
    assert derived["memory_grant_waste_pct"] == pytest.approx(90.0)
    assert derived["memory_grant_waste_pct"] > MEMORY_GRANT_WASTE_WARN_PCT
    assert "memory_grant_waste_pct" in {f["key"] for f in flag_metrics(derived)}


def test_memory_grant_waste_is_none_without_grant_data():
    assert derive_metrics(_row())["memory_grant_waste_pct"] is None


# --- Index önerileri -----------------------------------------------------------------------


def _missing(**over):
    base = dict(
        schema_name="dbo",
        table_name="orders",
        equality_columns="[status]",
        inequality_columns="[created_at]",
        included_columns="[total], [customer_id]",
        user_seeks=30,
        user_scans=0,
        avg_user_impact=77.6,
        avg_total_user_cost=0.29,
        improvement_measure=6.84,
    )
    base.update(over)
    return base


def test_generated_ddl_puts_equality_columns_first():
    """Kolon SIRASI index'in işe yarayıp yaramamasını belirler; SQL Server'ın önerisi bu
    sırayı garanti etmiyor."""
    ddl = index_ddl(_missing())
    assert ddl is not None
    assert "([status], [created_at])" in ddl
    assert "INCLUDE ([total], [customer_id])" in ddl


def test_generated_ddl_is_none_without_key_columns():
    """Anahtar kolonu olmayan bir öneriden index üretmek, çalışmayan bir DDL vermek olurdu."""
    assert index_ddl(_missing(equality_columns=None, inequality_columns=None)) is None


def test_online_option_is_only_a_comment():
    """`WITH (ONLINE = ON)` yalnızca Enterprise'da var; komuta gömmek Standard sürümde
    hata verirdi."""
    ddl = index_ddl(_missing())
    assert "-- Enterprise" in ddl
    assert not ddl.replace("-- Enterprise sürümde kilitsiz oluşturmak için: WITH (ONLINE = ON)", "").strip().endswith("WITH (ONLINE = ON)")


def test_missing_index_advice_warns_that_suggestions_are_raw():
    """SQL Server'ın önerilerini körlemesine uygulamak, yazma maliyetini patlatan bir index
    yığını bırakır — öneri bunu söylemeli."""
    advice = advice_for_missing_index(_missing())
    cautions = " ".join(advice.cautions)
    assert "HAM" in cautions
    # `.lower()` KULLANILMIYOR: Türkçede "SIFIRLANIR".lower() "sifirlanir" verir (noktasız I
    # ASCII i'ye düşer), yani "sıfırlan" araması tutmaz. Bu tuzak projede daha önce de
    # yaşandı; metin olduğu gibi aranıyor.
    assert "SIFIRLANIR" in cautions
    assert advice.why and advice.steps and advice.verification


def test_unused_index_advice_warns_when_counters_are_young():
    """Sayaçlar üç saatlikse "hiç kullanılmıyor" iddiası yanlıştır: haftada bir çalışan bir
    rapor o index'i kullanıyor olabilir."""
    young = advice_for_unused_index(
        {"schema_name": "dbo", "table_name": "orders", "index_name": "ix",
         "stats_age_seconds": 3 * 3600, "idx_tup_fetch": 100}
    )
    assert "DİKKAT" in young.why

    old = advice_for_unused_index(
        {"schema_name": "dbo", "table_name": "orders", "index_name": "ix",
         "stats_age_seconds": 30 * 86400, "idx_tup_fetch": 100}
    )
    assert "DİKKAT" not in old.why


def test_unused_index_advice_disables_before_dropping():
    """Silmek geri alınamaz; devre dışı bırakmak tek komutla geri alınır."""
    advice = advice_for_unused_index(
        {"schema_name": "dbo", "table_name": "orders", "index_name": "ix",
         "stats_age_seconds": 30 * 86400, "idx_tup_fetch": 100}
    )
    commands = [s.command or "" for s in advice.steps]
    disable_at = next(i for i, c in enumerate(commands) if "DISABLE" in c)
    drop_at = next(i for i, c in enumerate(commands) if "DROP INDEX" in c)
    assert disable_at < drop_at
