"""Sorgu metrik sözlüğü ve türetilmiş göstergeler (Faz 29 İŞ 2a).

Bu dosyanın koruduğu fikirler:

1. **Ham sayı tanı değildir.** Karar oranlara göre veriliyor; ham sayaçlar yalnızca kanıt.
2. **Ölçüm yokluğu ile sıfır farklı şeyler.** `track_io_timing = off` iken I/O süresi 0
   gelir; bunu "I/O beklemesi yok" diye sunmak yanlış teşhis üretir.
3. **Her metriğin bir anlamı ve bir eşiği var** — ve ikisi de tek yerden geliyor.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.query_metrics import (
    CACHE_HIT_WARN_PCT,
    INSTABILITY_RATIO_WARN,
    IO_TIME_SHARE_WARN_PCT,
    JIT_TIME_SHARE_WARN_PCT,
    METRIC_MEANINGS,
    PLAN_TIME_SHARE_WARN_PCT,
    derive_metrics,
    flag_metrics,
    io_timing_measured,
    metric_dictionary,
)


def _row(**over):
    base = dict(
        calls=100,
        total_time_ms=1000.0,
        mean_time_ms=10.0,
        rows=500,
        stddev_time_ms=1.0,
        min_time_ms=8.0,
        max_time_ms=14.0,
        shared_blks_hit=9900,
        shared_blks_read=100,
        shared_blks_dirtied=0,
        shared_blks_written=0,
        temp_blks_read=0,
        temp_blks_written=0,
        blk_read_time_ms=5.0,
        blk_write_time_ms=0.0,
        wal_records=0,
        wal_fpi=0,
        wal_bytes=0.0,
        plans=0,
        total_plan_time_ms=0.0,
        jit_time_ms=0.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


# --- Sözlük -------------------------------------------------------------------------------


def test_every_metric_explains_itself_and_when_it_is_a_problem():
    """Ham sayı tek başına kullanıcıya hiçbir şey söylemiyor. "Ne zaman sorun" cümlesi
    olmayan bir metrik, ekranda yer kaplamaktan başka iş yapmaz."""
    assert METRIC_MEANINGS
    for meaning in METRIC_MEANINGS:
        assert meaning.label
        assert len(meaning.meaning) > 20, meaning.key
        assert len(meaning.when_problem) > 40, meaning.key


def test_metric_dictionary_is_serialisable_for_the_ui():
    rows = metric_dictionary()
    assert rows and all({"key", "label", "unit", "meaning", "when_problem"} <= set(r) for r in rows)


# --- Ölçüm var mı -------------------------------------------------------------------------


def test_zero_io_time_with_disk_reads_means_measurement_is_off():
    """Diskten 500 blok okunmuş ama süre sıfır: `track_io_timing` kapalı demektir."""
    assert io_timing_measured(_row(shared_blks_read=500, blk_read_time_ms=0.0)) is False


def test_zero_io_time_without_disk_reads_is_a_real_zero():
    """Sorgu diske hiç gitmediyse sıfır süre DOĞRUDUR."""
    assert io_timing_measured(_row(shared_blks_read=0, blk_read_time_ms=0.0)) is True


def test_missing_column_is_not_treated_as_measured():
    """Eski bir örnekte sütun hiç yoksa ölçüm var sayılmamalı."""
    assert io_timing_measured(SimpleNamespace(shared_blks_read=0)) is False


def test_io_share_is_none_when_measurement_is_off():
    """0 göstermek "I/O beklemesi yok" demek olurdu; doğru cevap "bilinmiyor"."""
    derived = derive_metrics(_row(shared_blks_read=500, blk_read_time_ms=0.0))
    assert derived["io_time_share_pct"] is None
    assert derived["io_time_measured"] is False


# --- Türetme ------------------------------------------------------------------------------


def test_total_share_needs_a_denominator_and_is_not_invented():
    assert derive_metrics(_row())["total_share_pct"] is None
    derived = derive_metrics(_row(total_time_ms=250.0), total_time_all_ms=1000.0)
    assert derived["total_share_pct"] == pytest.approx(25.0)


def test_instability_is_relative_to_the_mean():
    """5 ms ortalamada 5 ms sapma ile 5 saniye ortalamada 5 ms sapma bambaşka şeyler."""
    unstable = derive_metrics(_row(mean_time_ms=10.0, stddev_time_ms=30.0))
    stable = derive_metrics(_row(mean_time_ms=5000.0, stddev_time_ms=30.0))
    assert unstable["instability_ratio"] == pytest.approx(3.0)
    assert stable["instability_ratio"] == pytest.approx(0.01)


def test_plan_share_uses_plan_plus_execution_as_the_denominator():
    """Yürütmeye göre hesaplamak, yürütmesi çok kısa sorgularda %1000 gibi anlamsız sayılar
    üretirdi."""
    derived = derive_metrics(_row(total_time_ms=100.0, total_plan_time_ms=100.0))
    assert derived["plan_time_share_pct"] == pytest.approx(50.0)


def test_wal_and_rows_are_reported_per_call():
    derived = derive_metrics(_row(calls=100, wal_bytes=50_000.0, rows=1000))
    assert derived["wal_bytes_per_call"] == pytest.approx(500.0)
    assert derived["rows_per_call"] == pytest.approx(10.0)


def test_fpi_ratio_needs_wal_records():
    assert derive_metrics(_row(wal_records=0, wal_fpi=5))["fpi_ratio"] is None
    assert derive_metrics(_row(wal_records=100, wal_fpi=25))["fpi_ratio"] == pytest.approx(0.25)


def test_zero_calls_never_divides_by_zero():
    derived = derive_metrics(_row(calls=0))
    assert derived["rows_per_call"] is None
    assert derived["wal_bytes_per_call"] is None


# --- Eşikler ------------------------------------------------------------------------------


def _flag_keys(row, **kwargs):
    return {f["key"] for f in flag_metrics(derive_metrics(row, **kwargs))}


def test_a_healthy_query_raises_no_flags():
    assert _flag_keys(_row()) == set()


def test_unstable_query_is_flagged():
    keys = _flag_keys(_row(mean_time_ms=10.0, stddev_time_ms=10.0 * INSTABILITY_RATIO_WARN + 1))
    assert "instability_ratio" in keys


def test_io_bound_query_is_flagged():
    row = _row(total_time_ms=1000.0, blk_read_time_ms=IO_TIME_SHARE_WARN_PCT * 10 + 10)
    assert "io_time_share_pct" in _flag_keys(row)


def test_low_cache_hit_is_flagged():
    row = _row(shared_blks_hit=80, shared_blks_read=20)
    assert derive_metrics(row)["cache_hit_pct"] < CACHE_HIT_WARN_PCT
    assert "cache_hit_pct" in _flag_keys(row)


def test_temp_file_usage_is_always_flagged():
    """Sıfırdan büyük her değer work_mem yetersizliğini gösteriyor; eşiği yok."""
    assert "temp_blocks" in _flag_keys(_row(temp_blks_written=1))


def test_planning_overhead_is_flagged():
    row = _row(total_time_ms=100.0, total_plan_time_ms=100.0)
    assert derive_metrics(row)["plan_time_share_pct"] > PLAN_TIME_SHARE_WARN_PCT
    assert "plan_time_share_pct" in _flag_keys(row)


def test_jit_overhead_is_flagged():
    row = _row(total_time_ms=100.0, jit_time_ms=JIT_TIME_SHARE_WARN_PCT + 5)
    assert "jit_time_share_pct" in _flag_keys(row)


def test_a_dominant_query_is_flagged_as_the_priority():
    keys = _flag_keys(_row(total_time_ms=500.0), total_time_all_ms=1000.0)
    assert "total_share_pct" in keys


def test_every_flag_carries_its_explanation():
    """Vurgulanan bir metrik "ne yapacağım" sorusunu cevaplamıyorsa vurgulamanın anlamı yok."""
    flags = flag_metrics(derive_metrics(_row(temp_blks_written=10, shared_blks_hit=50,
                                             shared_blks_read=50)))
    assert flags
    for flag in flags:
        assert flag["meaning"] and flag["when_problem"]
