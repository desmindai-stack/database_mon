"""PostgreSQL sürüm yetenek matrisi — hangi metrik hangi sürümde NEREDEN alınıyor.

Faz 27 İŞ 4 (alternatif kaynak) ve İŞ 5 (PG 15-18 tam destek) bu tablodan besleniyor.

SORUN: DPA ekranında şu yazıyordu — *"buffers_backend_per_sec — PostgreSQL 17+ sürümünde
pg_stat_bgwriter'dan kaldırıldı; doğrudan bir karşılığı yok."* **Bu eksikti: karşılığı VAR.**
PostgreSQL 17 sütunu kaldırdı ama aynı bilgi `pg_stat_io` içinde context ve backend_type
bazında raporlanıyor. Yani metrik toplanamıyor değildi; sadece BAŞKA yerden alınması
gerekiyordu.

"Desteklenmiyor" demek, kullanıcının ekranında bir eksiklik bırakmak ve onu başka bir araca
yönlendirmektir. Alternatifi varken bunu söylemek yanlıştır — bu yüzden yetenek tablosu
"kaynak öncelik listesi" olarak kuruldu: sürüme uyan İLK kaynak kullanılıyor, hiçbiri
uymuyorsa NEDENİ yazılıyor.

TEK KAYNAK: sürüm dallanması koda dağıtılmıyor, burada duruyor. Öncesinde eşikler
`collectors/postgresql.py` içinde sabit olarak yazılıydı ve README'de anlatılmıyordu; yeni
bir sürüm çıktığında nereye bakılacağı belli değildi.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Sürüm eşikleri (MMmmpp: 170000 == 17.0) ------------------------------------------------

PG_12 = 120_000
PG_13 = 130_000
PG_14 = 140_000
PG_15 = 150_000
PG_16 = 160_000
PG_17 = 170_000
PG_18 = 180_000

#: dbace'in desteklediği en düşük sürüm. Altındaki sunucuya yine bağlanılıyor ama en yakın
#: davranışla ve uyarı log'uyla — reddetmek, çalışabilecek bir kurulumu boşuna engellerdi.
MIN_SUPPORTED = PG_12

#: Test edilen en yüksek sürüm. Üstündeki sunucularda en yeni dal kullanılıyor: PostgreSQL'in
#: katalog değişiklikleri neredeyse her zaman eklemeli olduğu için bu, reddetmekten güvenli.
MAX_TESTED = PG_18


@dataclass(frozen=True)
class MetricSource:
    """Bir metriğin belirli bir sürüm aralığındaki kaynağı."""

    #: Kullanıcıya gösterilen kaynak adı ("pg_stat_bgwriter", "pg_stat_io").
    view: str
    min_version: int
    #: Bu sürümden İTİBAREN artık geçerli değil (dışlayıcı). None = hâlâ geçerli.
    removed_in: int | None = None
    #: Kaynak birincil olmadığında kullanıcıya söylenecek not.
    note: str | None = None

    def applies_to(self, version_num: int) -> bool:
        if version_num < self.min_version:
            return False
        return self.removed_in is None or version_num < self.removed_in


@dataclass(frozen=True)
class MetricCapability:
    """Bir metriğin sürüme göre kaynak öncelik listesi."""

    key: str
    sources: tuple[MetricSource, ...]
    #: Hiçbir kaynak uymadığında yazılacak açıklama. `{version}` ile sürüm gömülüyor.
    unavailable_template: str

    def source_for(self, version_num: int) -> MetricSource | None:
        for source in self.sources:
            if source.applies_to(version_num):
                return source
        return None


_BGWRITER = "pg_stat_bgwriter"
_CHECKPOINTER = "pg_stat_checkpointer"
_STAT_IO = "pg_stat_io"

#: PostgreSQL 17, checkpoint sütunlarını `pg_stat_bgwriter`'dan `pg_stat_checkpointer`'a
#: taşıdı ve adlarını değiştirdi; `buffers_backend`/`buffers_backend_fsync` sütunlarını ise
#: TAMAMEN kaldırdı. İkincisinin karşılığı `pg_stat_io`'da duruyor.
_BACKEND_IO_NOTE = (
    "PostgreSQL 17'de pg_stat_bgwriter'dan kaldırıldı; aynı bilgi pg_stat_io'dan "
    "(arka plan süreçleri dışındaki yazmalar) alınıyor."
)

CAPABILITIES: dict[str, MetricCapability] = {
    # --- Checkpoint metrikleri: 17'de görünüm değişti, bilgi kaybolmadı ---
    "checkpoints_timed": MetricCapability(
        "checkpoints_timed",
        (
            MetricSource(_CHECKPOINTER, PG_17, note="17'de pg_stat_bgwriter'dan taşındı (num_timed)."),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Checkpoint sayacı bu sürümde okunamıyor (sunucu: {version}).",
    ),
    "checkpoints_req": MetricCapability(
        "checkpoints_req",
        (
            MetricSource(_CHECKPOINTER, PG_17, note="17'de num_requested olarak yeniden adlandırıldı."),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Checkpoint sayacı bu sürümde okunamıyor (sunucu: {version}).",
    ),
    "checkpoint_write_time_ms": MetricCapability(
        "checkpoint_write_time_ms",
        (
            MetricSource(_CHECKPOINTER, PG_17),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Checkpoint süre ölçümü bu sürümde okunamıyor (sunucu: {version}).",
    ),
    "checkpoint_sync_time_ms": MetricCapability(
        "checkpoint_sync_time_ms",
        (
            MetricSource(_CHECKPOINTER, PG_17),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Checkpoint süre ölçümü bu sürümde okunamıyor (sunucu: {version}).",
    ),
    "buffers_checkpoint_per_sec": MetricCapability(
        "buffers_checkpoint_per_sec",
        (
            MetricSource(_CHECKPOINTER, PG_17, note="17'de buffers_written olarak yeniden adlandırıldı."),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Checkpoint buffer sayacı bu sürümde okunamıyor (sunucu: {version}).",
    ),
    # buffers_clean ve buffers_alloc 17'de de pg_stat_bgwriter'da KALDI.
    "buffers_clean_per_sec": MetricCapability(
        "buffers_clean_per_sec",
        (MetricSource(_BGWRITER, MIN_SUPPORTED),),
        "pg_stat_bgwriter okunamıyor (sunucu: {version}).",
    ),
    "buffers_alloc_per_sec": MetricCapability(
        "buffers_alloc_per_sec",
        (MetricSource(_BGWRITER, MIN_SUPPORTED),),
        "pg_stat_bgwriter okunamıyor (sunucu: {version}).",
    ),
    # --- BİLDİRİLEN EKSİK: karşılığı var ---
    "buffers_backend_per_sec": MetricCapability(
        "buffers_backend_per_sec",
        (
            MetricSource(_STAT_IO, PG_17, note=_BACKEND_IO_NOTE),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Backend yazma sayacı bu sürümde okunamıyor (sunucu: {version}).",
    ),
    "buffers_backend_fsync_per_sec": MetricCapability(
        "buffers_backend_fsync_per_sec",
        (
            MetricSource(_STAT_IO, PG_17, note=_BACKEND_IO_NOTE),
            MetricSource(_BGWRITER, MIN_SUPPORTED, removed_in=PG_17),
        ),
        "Backend fsync sayacı bu sürümde okunamıyor (sunucu: {version}).",
    ),
    # --- pg_stat_io metrikleri: 16 öncesinde GERÇEKTEN karşılığı yok ---
    "io_reads_per_sec": MetricCapability(
        "io_reads_per_sec",
        (MetricSource(_STAT_IO, PG_16),),
        "pg_stat_io PostgreSQL 16 ile geldi; bu sunucuda ({version}) sürüm bazlı I/O kırılımı "
        "yok. En yakın bilgi cache hit oranı ve shared_blks sayaçlarında.",
    ),
    "io_writes_per_sec": MetricCapability(
        "io_writes_per_sec",
        (MetricSource(_STAT_IO, PG_16),),
        "pg_stat_io PostgreSQL 16 ile geldi; bu sunucuda ({version}) yok.",
    ),
    "io_extends_per_sec": MetricCapability(
        "io_extends_per_sec",
        (MetricSource(_STAT_IO, PG_16),),
        "pg_stat_io PostgreSQL 16 ile geldi; bu sunucuda ({version}) yok.",
    ),
    # PostgreSQL 18, `op_bytes` sütununu KALDIRDI ve yerine gerçek bayt sayaçları koydu
    # (read_bytes / write_bytes / extend_bytes). Eski sütunu sormaya devam etmek, PG 18'de
    # pg_stat_io sorgusunun TAMAMINI düşürürdü — yani io_reads/io_writes de kaybolurdu.
    # Sürüm aralığı bu yüzden 16-17 ile sınırlı.
    "io_op_bytes": MetricCapability(
        "io_op_bytes",
        (MetricSource(_STAT_IO, PG_16, removed_in=PG_18),),
        "pg_stat_io.op_bytes yalnızca PostgreSQL 16-17'de var (bu sunucu: {version}). "
        "16 öncesinde pg_stat_io hiç yok; 18'den itibaren sütun kaldırıldı ve yerine gerçek "
        "bayt sayaçları geldi — dbace onları io_read_bytes_per_sec / io_write_bytes_per_sec "
        "olarak topluyor.",
    ),
    # PostgreSQL 18'in getirdiği gerçek bayt sayaçları. `op_bytes` bir işlem BAŞINA bayt
    # veriyordu (çarpma gerekiyordu); bunlar doğrudan toplam bayt — daha kullanışlı.
    "io_read_bytes_per_sec": MetricCapability(
        "io_read_bytes_per_sec",
        (MetricSource(_STAT_IO, PG_18, note="PostgreSQL 18 ile geldi (op_bytes'ın yerine)."),),
        "Gerçek bayt sayaçları PostgreSQL 18 ile geldi (bu sunucu: {version}). "
        "16-17'de yaklaşık değer io_reads × io_op_bytes ile hesaplanabilir.",
    ),
    "io_write_bytes_per_sec": MetricCapability(
        "io_write_bytes_per_sec",
        (MetricSource(_STAT_IO, PG_18, note="PostgreSQL 18 ile geldi (op_bytes'ın yerine)."),),
        "Gerçek bayt sayaçları PostgreSQL 18 ile geldi (bu sunucu: {version}).",
    ),
}


def source_for(metric_key: str, version_num: int) -> MetricSource | None:
    capability = CAPABILITIES.get(metric_key)
    return capability.source_for(version_num) if capability else None


def unavailable_reason(metric_key: str, version_num: int) -> str | None:
    """Metrik bu sürümde alınamıyorsa NEDENİ; alınabiliyorsa None.

    Sebep, "hangi sürümde geldi/gitti" bilgisini de taşıyor: kullanıcı sürüm yükseltmesinin
    ne kazandıracağını bilsin.
    """
    capability = CAPABILITIES.get(metric_key)
    if capability is None:
        return None
    if capability.source_for(version_num) is not None:
        return None
    return capability.unavailable_template.format(version=format_version(version_num))


def format_version(version_num: int) -> str:
    """170004 → '17.4'. Ham sayı kullanıcıya hiçbir şey söylemiyor."""
    major = version_num // 10_000
    minor = version_num % 10_000
    return f"{major}.{minor}" if minor else str(major)


def capability_matrix(version_num: int) -> dict[str, dict[str, str | None]]:
    """Bu sürümde her metriğin nereden alındığı — API ve README için.

    Sürüme göre TÜRETİLİYOR, saklanmıyor: saklanan bir tablo sunucu yükseltildiğinde
    sessizce eskir.
    """
    matrix: dict[str, dict[str, str | None]] = {}
    for key, capability in CAPABILITIES.items():
        source = capability.source_for(version_num)
        matrix[key] = {
            "source": source.view if source else None,
            "note": source.note if source else None,
            "unavailable_reason": (
                None if source else capability.unavailable_template.format(version=format_version(version_num))
            ),
        }
    return matrix


def version_support_note(version_num: int) -> str | None:
    """Sürümün destek aralığının dışında olup olmadığı — uyarı metni ya da None."""
    if version_num < MIN_SUPPORTED:
        return (
            f"Sunucu sürümü {format_version(version_num)}, dbace'in desteklediği en düşük "
            f"sürümün ({format_version(MIN_SUPPORTED)}) altında. Toplama en yakın davranışla "
            "deneniyor ama bazı sorgular başarısız olabilir."
        )
    if version_num >= MAX_TESTED + 10_000:
        return (
            f"Sunucu sürümü {format_version(version_num)}, dbace'in test edildiği en yüksek "
            f"sürümün ({format_version(MAX_TESTED)}) üstünde. En yeni dal kullanılıyor; "
            "PostgreSQL katalog değişiklikleri genelde eklemeli olduğu için bu güvenli, ama "
            "yeni metrikler kullanılmıyor olabilir."
        )
    return None
