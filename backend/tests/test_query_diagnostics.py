"""Faz 16 İŞ 3 — kaynak bazlı sorgu tanısı. Her dal için bariz bir örnek veriyor ve doğru
resource/confidence çıktığını kanıtlıyor; "lock" sınıfının HER ZAMAN confidence="inferred"
olduğunu ayrıca doğruluyor (dbace per-query kilit süresi ölçmüyor — bkz. SORULAR.md)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.query_diagnostics import diagnose_query, diagnose_queries


def _row(**overrides):
    base = dict(
        queryid="abc",
        query="SELECT 1",
        calls=10,
        mean_time_ms=100.0,
        total_time_ms=1000.0,
        shared_blks_hit=1000,
        shared_blks_read=0,
        local_blks_hit=0,
        local_blks_read=0,
        temp_blks_read=0,
        temp_blks_written=0,
        exec_user_time=90.0,
        exec_sys_time=5.0,
        # Faz 29 İŞ 2a alanları. Varsayılan: I/O süresi ölçülmüş ve sıfır (sorgu diske hiç
        # gitmemiş) — çünkü varsayılan satırda shared_blks_read = 0.
        blk_read_time_ms=0.0,
        blk_write_time_ms=0.0,
        stddev_time_ms=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_temp_file_usage_is_memory_bottleneck():
    row = _row(temp_blks_written=500)
    d = diagnose_query(row)
    assert d.resource == "memory"
    assert d.confidence == "observed"
    assert "geçici dosya" in d.reason


def test_high_disk_read_ratio_is_io_bottleneck_but_only_inferred():
    """Blok ORANINDAN çıkarılan I/O teşhisi "observed" değil "inferred" (Faz 29 İŞ 2a).

    Çok sayıda bloğu HIZLI bir diskten okumak I/O darboğazı değildir; oran tek başına
    beklemeyi kanıtlamaz. Bu yola artık yalnızca `track_io_timing` kapalıyken düşülüyor ve
    kullanıcıya çıkarım olduğu söyleniyor.
    """
    row = _row(shared_blks_hit=100, shared_blks_read=500, exec_user_time=None, exec_sys_time=None)
    d = diagnose_query(row)
    assert d.resource == "io"
    assert d.confidence == "inferred"
    assert "track_io_timing" in d.reason


def test_measured_io_time_beats_the_block_ratio_inference():
    """ÖLÇÜM ÇIKARIMDAN ÖNCE GELİR: `blk_read_time` sorgunun diskte beklediği süreyi
    doğrudan veriyor."""
    row = _row(
        shared_blks_hit=1000,
        shared_blks_read=200,
        blk_read_time_ms=600.0,
        blk_write_time_ms=0.0,
        total_time_ms=1000.0,
        exec_user_time=None,
        exec_sys_time=None,
    )
    d = diagnose_query(row)
    assert d.resource == "io"
    assert d.confidence == "observed"
    assert "diski beklemekle" in d.reason


def test_io_timing_off_is_not_read_as_no_io_wait():
    """`track_io_timing = off` iken sütun 0 gelir. Sıfırı "I/O beklemesi yok" saymak yanlış
    teşhis üretirdi; diskten blok okunmuşken süre sıfırsa ölçüm kapalı demektir."""
    from app.domain.query_metrics import io_timing_measured

    row = _row(shared_blks_read=500, blk_read_time_ms=0.0)
    assert io_timing_measured(row) is False
    # Gerçekten diske hiç gitmemiş bir sorguda sıfır süre DOĞRUDUR ve ölçüm sayılıyor.
    assert io_timing_measured(_row(shared_blks_read=0, blk_read_time_ms=0.0)) is True


def test_cpu_dominant_time_is_cpu_bottleneck():
    row = _row(shared_blks_hit=1000, shared_blks_read=0, mean_time_ms=100.0, exec_user_time=90.0, exec_sys_time=5.0)
    d = diagnose_query(row)
    assert d.resource == "cpu"
    assert d.confidence == "observed"


def test_large_unexplained_gap_is_inferred_lock():
    # CPU only explains 20ms of a 100ms query, no disk reads — the remaining 80ms isn't CPU or
    # I/O, so it's flagged as a possible lock/wait, but never asserted as certain.
    row = _row(shared_blks_hit=1000, shared_blks_read=0, mean_time_ms=100.0, exec_user_time=15.0, exec_sys_time=5.0)
    d = diagnose_query(row)
    assert d.resource == "lock"
    assert d.confidence == "inferred"
    assert "Activity" in d.reason


def test_missing_exec_time_columns_is_unknown_not_fabricated():
    row = _row(shared_blks_hit=1000, shared_blks_read=0, exec_user_time=None, exec_sys_time=None)
    d = diagnose_query(row)
    assert d.resource == "unknown"
    assert d.confidence == "inferred"


def test_diagnose_queries_preserves_order():
    rows = [_row(queryid="a"), _row(queryid="b", temp_blks_written=10)]
    diagnoses = diagnose_queries(rows)
    assert [d.queryid for d in diagnoses] == ["a", "b"]
