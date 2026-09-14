"""Tablo erişim kalıpları (Faz 29 İŞ 2b).

Bu dosyanın koruduğu asıl fikir: **küçük tablodaki sıralı tarama sorun değildir.**

En kolay hata, `seq_scan` sayacı yüksek diye bulgu üretmek olurdu. 1000 satırlık bir tabloda
sıralı tarama planlayıcının DOĞRU tercihidir — tüm tabloyu okumak index'ten ucuzdur. O
tercihleri bulgu diye göstermek, listeyi gürültüye boğup gerçek sinyali görünmez yapardı.
Bu yüzden eşik "kaç kez tarandı" değil, **tarama başına kaç satır okundu** üzerinden.
"""

from __future__ import annotations

import pytest

from app.domain.table_access import (
    CACHE_HIT_WARN_PCT,
    CACHE_MIN_BLOCKS,
    HOT_MIN_UPDATES,
    SEQ_SCAN_ROWS_WARN,
    STALE_STATS_MIN_ROWS,
    analyze_table_access,
    derive_table_access,
)
from app.services.table_access_advice import advice_for_signal


def _row(**over):
    base = dict(
        schema_name="app",
        table_name="orders",
        seq_scan=0,
        seq_tup_read=0,
        idx_scan=1000,
        idx_tup_fetch=5000,
        n_live_tup=500_000,
        n_tup_upd=0,
        n_tup_hot_upd=0,
        n_mod_since_analyze=0,
        heap_blks_hit=1_000_000,
        heap_blks_read=1000,
        idx_blks_hit=100_000,
        idx_blks_read=10,
        table_bytes=100_000_000,
    )
    base.update(over)
    return base


def _keys(row):
    return {s.key for s in analyze_table_access(row)}


# --- Gürültü üretmeme ----------------------------------------------------------------------


def test_a_healthy_table_produces_no_signal():
    assert _keys(_row()) == set()


def test_sequential_scans_on_a_small_table_are_not_a_problem():
    """1000 satırlık bir tabloyu 10.000 kez sıralı taramak NORMALDİR: planlayıcı index'i
    bilerek kullanmaz, çünkü tüm tabloyu okumak daha ucuzdur."""
    row = _row(seq_scan=10_000, seq_tup_read=10_000 * 800, idx_scan=0, n_live_tup=800)
    assert "seq_scan_dominant" not in _keys(row)


def test_large_scans_on_a_large_table_are_flagged():
    row = _row(seq_scan=100, seq_tup_read=100 * (SEQ_SCAN_ROWS_WARN + 1), idx_scan=0)
    assert "seq_scan_dominant" in _keys(row)


def test_large_scans_are_not_flagged_when_index_access_dominates():
    """Tabloya çoğunlukla index'le giriliyorsa, arada bir yapılan büyük tarama (ör. gece
    raporu) bulgu değildir."""
    row = _row(seq_scan=5, seq_tup_read=5 * (SEQ_SCAN_ROWS_WARN + 1), idx_scan=100_000)
    assert "seq_scan_dominant" not in _keys(row)


def test_cache_hit_needs_enough_blocks_to_be_meaningful():
    """10 bloklu bir tabloda %50 isabet istatistiksel gürültüdür."""
    tiny = _row(heap_blks_hit=5, heap_blks_read=5)
    assert derive_table_access(tiny)["heap_cache_hit_pct"] == 50.0
    assert "low_table_cache_hit" not in _keys(tiny)

    big = _row(heap_blks_hit=CACHE_MIN_BLOCKS, heap_blks_read=CACHE_MIN_BLOCKS)
    assert "low_table_cache_hit" in _keys(big)


def test_hot_ratio_needs_enough_updates():
    few = _row(n_tup_upd=10, n_tup_hot_upd=0)
    assert "low_hot_update_ratio" not in _keys(few)
    many = _row(n_tup_upd=HOT_MIN_UPDATES, n_tup_hot_upd=0)
    assert "low_hot_update_ratio" in _keys(many)


def test_stale_statistics_measures_change_volume_not_time():
    """Az yazılan bir tabloda 3 gün eski istatistik sorun değil; çok yazılan bir tabloda
    1 saat eski istatistik sorundur. Ölçü zaman değil DEĞİŞİM."""
    quiet = _row(n_live_tup=1_000_000, n_mod_since_analyze=100)
    assert "stale_statistics" not in _keys(quiet)
    busy = _row(n_live_tup=100_000, n_mod_since_analyze=max(STALE_STATS_MIN_ROWS, 50_000))
    assert "stale_statistics" in _keys(busy)


# --- Türetme -------------------------------------------------------------------------------


def test_rows_per_seq_scan_is_per_scan_not_total():
    derived = derive_table_access(_row(seq_scan=10, seq_tup_read=1000))
    assert derived["rows_per_seq_scan"] == pytest.approx(100.0)


def test_ratios_are_none_when_there_is_nothing_to_divide():
    derived = derive_table_access(
        _row(seq_scan=0, idx_scan=0, n_tup_upd=0, n_live_tup=0,
             heap_blks_hit=0, heap_blks_read=0, idx_blks_hit=0, idx_blks_read=0)
    )
    assert derived["rows_per_seq_scan"] is None
    assert derived["seq_scan_share_pct"] is None
    assert derived["hot_update_pct"] is None
    assert derived["heap_cache_hit_pct"] is None
    assert derived["modified_since_analyze_pct"] is None


# --- Anlatım ve öneri ----------------------------------------------------------------------


def test_every_signal_explains_itself():
    row = _row(
        seq_scan=100,
        seq_tup_read=100 * (SEQ_SCAN_ROWS_WARN + 1),
        idx_scan=0,
        n_tup_upd=HOT_MIN_UPDATES,
        n_tup_hot_upd=0,
        n_live_tup=100_000,
        n_mod_since_analyze=50_000,
        heap_blks_hit=CACHE_MIN_BLOCKS,
        heap_blks_read=CACHE_MIN_BLOCKS,
    )
    signals = analyze_table_access(row)
    assert len(signals) == 4
    for signal in signals:
        assert len(signal.meaning) > 30, signal.key
        assert len(signal.when_problem) > 60, signal.key
        assert signal.evidence.get("metric")


def test_every_signal_has_a_five_part_advice():
    """CLAUDE.md: neden + numaralı adımlar + komut + dikkat + doğrulama."""
    for key in ("seq_scan_dominant", "low_table_cache_hit", "low_hot_update_ratio",
                "stale_statistics"):
        advice = advice_for_signal(key, "app", "orders", {"value": 42})
        assert advice.why and len(advice.why) > 50, key
        assert advice.steps, key
        assert any(step.command for step in advice.steps), key
        assert advice.cautions, key
        assert advice.verification, key


def test_seq_scan_advice_does_not_pretend_to_know_the_column():
    """dbace hangi kolona index gerektiğini tablo sayacından BİLEMEZ; `seq_scan` hangi WHERE
    koşuluyla tarandığını taşımıyor. Bilinmeyeni biliyormuş gibi sunmak yanlış index
    kurdurmaktan başka işe yaramaz."""
    advice = advice_for_signal("seq_scan_dominant", "app", "orders", {"value": 50_000})
    text = advice.why + " ".join(s.action for s in advice.steps)
    assert "CREATE INDEX ON" not in text
    assert "index önerisi" in text


def test_an_unknown_signal_never_invents_advice():
    advice = advice_for_signal("boyle_bir_sinyal_yok", "app", "orders", {})
    assert advice.unavailable_reason
    assert not advice.steps
