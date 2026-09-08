"""PostgreSQL sürüm yetenek matrisi (Faz 27 İŞ 4).

BİLDİRİLEN EKSİK: DPA ekranında şu yazıyordu — *"buffers_backend_per_sec — PostgreSQL 17+
sürümünde pg_stat_bgwriter'dan kaldırıldı; doğrudan bir karşılığı yok."*

**Bu eksikti: karşılığı VAR.** PostgreSQL 17 sütunu kaldırdı ama aynı bilgi `pg_stat_io`
içinde `backend_type` ve `context` bazında raporlanıyor. Metrik toplanamıyor değildi; sadece
BAŞKA yerden alınması gerekiyordu.

"Desteklenmiyor" demek, kullanıcının ekranında bir eksiklik bırakmak ve onu başka bir araca
yönlendirmektir. Alternatifi varken bunu söylemek yanlıştır.
"""

from __future__ import annotations

import pytest

from app.domain.pg_capabilities import (
    CAPABILITIES,
    MAX_TESTED,
    MIN_SUPPORTED,
    PG_12,
    PG_13,
    PG_14,
    PG_15,
    PG_16,
    PG_17,
    PG_18,
    capability_matrix,
    format_version,
    source_for,
    unavailable_reason,
    version_support_note,
)

ALL_VERSIONS = [PG_12, PG_13, PG_14, PG_15, PG_16, PG_17, PG_18]


# --- Bildirilen eksiğin düzeltilmesi ------------------------------------------------------


def test_buffers_backend_has_a_source_on_pg17_and_18():
    """BİLDİRİLEN HATA. 17'de sütun kaldırıldı ama karşılığı pg_stat_io'da."""
    for version in (PG_17, PG_18):
        source = source_for("buffers_backend_per_sec", version)
        assert source is not None, f"PG {format_version(version)}: kaynak bulunamadı"
        assert source.view == "pg_stat_io"
        assert unavailable_reason("buffers_backend_per_sec", version) is None


def test_buffers_backend_comes_from_bgwriter_before_pg17():
    for version in (PG_12, PG_15, PG_16):
        source = source_for("buffers_backend_per_sec", version)
        assert source is not None and source.view == "pg_stat_bgwriter"


def test_the_alternative_source_carries_an_explanation():
    """Kullanıcı kaynağın neden değiştiğini görmeli; sessizce başka bir view'dan okumak
    "sayı neden farklı" sorusunu cevapsız bırakırdı."""
    source = source_for("buffers_backend_per_sec", PG_17)
    assert source.note and "pg_stat_io" in source.note


def test_backend_fsync_follows_the_same_path():
    assert source_for("buffers_backend_fsync_per_sec", PG_17).view == "pg_stat_io"
    assert source_for("buffers_backend_fsync_per_sec", PG_16).view == "pg_stat_bgwriter"


# --- Checkpoint metrikleri: 17'de taşındı, kaybolmadı --------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "checkpoints_timed",
        "checkpoints_req",
        "checkpoint_write_time_ms",
        "checkpoint_sync_time_ms",
        "buffers_checkpoint_per_sec",
    ],
)
def test_checkpoint_metrics_move_to_the_new_view_on_pg17(key):
    assert source_for(key, PG_16).view == "pg_stat_bgwriter"
    assert source_for(key, PG_17).view == "pg_stat_checkpointer"
    assert unavailable_reason(key, PG_17) is None


@pytest.mark.parametrize("key", ["buffers_clean_per_sec", "buffers_alloc_per_sec"])
def test_metrics_that_stayed_on_bgwriter_are_not_moved(key):
    """buffers_clean ve buffers_alloc 17'de de pg_stat_bgwriter'da KALDI. Onları da taşımak,
    var olmayan bir sütundan okumaya çalışmak olurdu."""
    for version in ALL_VERSIONS:
        assert source_for(key, version).view == "pg_stat_bgwriter"


# --- Gerçekten karşılığı olmayan metrikler --------------------------------------------------


@pytest.mark.parametrize(
    "key", ["io_reads_per_sec", "io_writes_per_sec", "io_extends_per_sec", "io_op_bytes"]
)
def test_pg_stat_io_metrics_are_genuinely_unavailable_before_pg16(key):
    """Bu metriklerin 16 öncesinde GERÇEKTEN karşılığı yok — orada "desteklenmiyor" demek
    doğru. Ayrım önemli: her "yok" yanlış değil, yalnızca alternatifi olan "yok"lar yanlış."""
    for version in (PG_12, PG_15):
        assert source_for(key, version) is None
        reason = unavailable_reason(key, version)
        assert reason and "16" in reason
    for version in (PG_16, PG_17, PG_18):
        assert source_for(key, version) is not None


def test_the_unavailable_reason_names_the_version_readably():
    """Ham sürüm numarası (150000) kullanıcıya hiçbir şey söylemiyor."""
    reason = unavailable_reason("io_reads_per_sec", 150004)
    assert "15.4" in reason
    assert "150004" not in reason


def test_the_unavailable_reason_says_what_the_closest_alternative_is():
    """"Yok" demek yetmez: kullanıcı en yakın bilgiyi nerede bulacağını bilmeli."""
    reason = unavailable_reason("io_reads_per_sec", PG_15)
    assert "cache hit" in reason.lower() or "shared_blks" in reason


# --- Matris bütünlüğü -----------------------------------------------------------------------


@pytest.mark.parametrize("version", ALL_VERSIONS)
def test_every_metric_either_has_a_source_or_a_stated_reason(version):
    """SESSİZ BOŞLUK YOK. Bir metrik ya bir kaynaktan geliyordur ya da neden gelmediği
    yazılıdır; üçüncü bir seçenek (boş görünüp sebebi olmayan) kullanıcıyı "özellik bozuk
    mu" sorusuyla baş başa bırakır."""
    matrix = capability_matrix(version)
    for key, info in matrix.items():
        assert info["source"] or info["unavailable_reason"], f"{key} @ {version}"
        # İkisi birden olamaz: kaynağı varsa "yok" denmemeli.
        assert not (info["source"] and info["unavailable_reason"]), f"{key} @ {version}"


def test_the_matrix_covers_every_declared_capability():
    matrix = capability_matrix(PG_17)
    assert set(matrix) == set(CAPABILITIES)


@pytest.mark.parametrize("version", ALL_VERSIONS)
def test_source_ranges_do_not_overlap(version):
    """İki kaynak aynı sürüme uyuyorsa öncelik listesi belirsiz hâle gelir ve metriğin
    nereden geldiği sürümden sürüme rastgele değişebilir."""
    for key, capability in CAPABILITIES.items():
        matching = [s for s in capability.sources if s.applies_to(version)]
        assert len(matching) <= 1, f"{key} @ {format_version(version)}: {len(matching)} kaynak"


# --- Sürüm aralığı uyarısı ------------------------------------------------------------------


def test_versions_in_range_produce_no_warning():
    for version in (MIN_SUPPORTED, PG_15, PG_16, PG_17, MAX_TESTED):
        assert version_support_note(version) is None


def test_a_too_old_server_is_warned_but_not_refused():
    """Reddetmek, çalışabilecek bir kurulumu boşuna engellerdi."""
    note = version_support_note(110000)
    assert note and "en düşük" in note


def test_a_newer_than_tested_server_says_the_branch_is_still_safe():
    """PostgreSQL katalog değişiklikleri neredeyse her zaman eklemeli; yeni bir sürümde
    "desteklenmiyor" deyip toplamayı durdurmak, çalışan bir kurulumu boşuna kırardı."""
    note = version_support_note(MAX_TESTED + 20_000)
    assert note and "yeni dal" in note


def test_version_formatting():
    assert format_version(170004) == "17.4"
    assert format_version(160000) == "16"
    assert format_version(120018) == "12.18"
