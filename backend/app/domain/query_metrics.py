"""Sorgu metriklerinin SÖZLÜĞÜ ve türetilmiş göstergeler (Faz 29 İŞ 2a).

## Neden sözlük

Ham sayı tanı değildir. `blk_read_time_ms = 10.86` kullanıcıya tek başına hiçbir şey
söylemiyor; "yürütme süresinin %28'i diski beklemekle geçti, yani bu sorgu CPU'ya değil
depolamaya bağlı" söylüyor. Her metriğin yanında **ne anlama geldiği** ve **ne zaman sorun**
olduğu duruyor; arayüz metni elle yazmıyor, buradan okuyor.

Bu, "aynı veriyi gösteren yerler tek gerçeklik kaynağından beslensin" kuralının bu alandaki
karşılığı: DPA tablosu, rapor bulgusu ve öneri metni aynı eşikleri ve aynı açıklamayı
kullanıyor.

## Neden türetilmiş göstergeler

pg_stat_statements kümülatif SAYAÇ tutuyor. "shared_blks_read = 28864" bir sunucuda çok, bir
başkasında az olabilir. Kararı veren şey oranlar: sürenin yüzde kaçı I/O, çağrı başına kaç
bayt WAL, sapma ortalamanın kaç katı. Ham sayılar da taşınıyor (kanıt için) ama eşik
oranlara uygulanıyor.

## Ölçüm yokluğu ile sıfır farklı şeyler

`track_io_timing = off` iken `blk_read_time` **0** gelir. Bunu "I/O beklemesi yok" diye
sunmak yanlış teşhis üretir — bu yüzden I/O göstergeleri yalnızca ölçümün açık olduğu
anlaşıldığında hesaplanıyor, aksi hâlde `None` ve nedeni yazılıyor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MetricMeaning:
    """Bir metriğin kullanıcıya anlatımı."""

    key: str
    label: str
    unit: str
    #: Bu sayı NE ÖLÇÜYOR.
    meaning: str
    #: NE ZAMAN sorun; eşik varsa burada geçiyor.
    when_problem: str


#: Ham sayaçlar ve türetilmiş oranlar için açıklamalar.
METRIC_MEANINGS: tuple[MetricMeaning, ...] = (
    MetricMeaning(
        "mean_time_ms",
        "Çağrı başına süre",
        "ms",
        "Sorgunun bir çağrısının ortalama yürütme süresi.",
        "Tek başına yanıltıcı: günde 3 kez çalışan 2 saniyelik bir sorgu, günde 2 milyon kez "
        "çalışan 5 milisaniyelik bir sorgudan çok daha az önemlidir. Toplam etki payıyla "
        "birlikte okunmalı.",
    ),
    MetricMeaning(
        "total_share_pct",
        "Toplam etki payı",
        "%",
        "Bu sorgunun, ölçülen tüm sorguların toplam süresi içindeki payı.",
        "Asıl öncelik göstergesi budur. %20'yi geçen tek bir sorgu, iyileştirildiğinde "
        "veritabanı yükünün beşte birini birden düşürür.",
    ),
    MetricMeaning(
        "instability_ratio",
        "Kararsızlık",
        "×",
        "Yürütme süresinin standart sapmasının ortalamaya oranı (stddev / mean).",
        "1'i geçtiğinde sorgu KARARSIZ demektir: bazen hızlı, bazen çok yavaş. Kullanıcıların "
        "'ara sıra takılıyor' şikâyetinin kaynağı genelde budur ve ortalamaya bakan hiçbir "
        "liste onu yakalayamaz. Sebebi genelde plan değişimi, kilit beklemesi ya da parametre "
        "değerine göre değişen seçicilik olur.",
    ),
    MetricMeaning(
        "io_time_share_pct",
        "I/O bekleme payı",
        "%",
        "Yürütme süresinin, blokları diskten okumak/yazmak için beklemekle geçen kısmı.",
        "%30'u geçtiğinde sorgu CPU'ya değil DEPOLAMAYA bağlıdır: sorguyu optimize etmek "
        "yerine index eklemek, cache'i büyütmek (shared_buffers) ya da daha hızlı disk "
        "kullanmak gerekir. Bu ölçüm `track_io_timing = on` gerektirir; kapalıyken "
        "hesaplanmaz (0 gösterilmez).",
    ),
    MetricMeaning(
        "cache_hit_pct",
        "Cache isabet oranı",
        "%",
        "Okunan blokların kaçta kaçının diske gitmeden bellekten karşılandığı.",
        "%95'in altı, bu sorgunun çalışma kümesinin shared_buffers'a sığmadığını gösterir. "
        "Düşük isabet tek başına kötü değildir — büyük bir tabloyu bir kez taramak da böyle "
        "görünür; tekrar eden bir sorguda ise anlamlıdır.",
    ),
    MetricMeaning(
        "temp_blocks",
        "Geçici dosya kullanımı",
        "blok",
        "Sıralama/hash işleminin belleğe sığmayıp diske taştığı blok sayısı.",
        "Sıfırdan büyük her değer `work_mem` yetersizliğini gösterir. Diske taşan bir sıralama, "
        "bellekte yapılana göre kat kat yavaştır ve bu, tek ayar değişikliğiyle çözülebilen "
        "en sık performans sorunudur.",
    ),
    MetricMeaning(
        "wal_bytes_per_call",
        "Çağrı başına WAL",
        "bayt",
        "Sorgunun çağrı başına ürettiği write-ahead log miktarı.",
        "Yüksek WAL üretimi replikasyon gecikmesini, yedek boyutunu ve disk yazma yükünü "
        "doğrudan artırır. Okuma sorgusunda WAL üretimi beklenmez; varsa sorgu aslında yazma "
        "yapıyor (ör. HOT güncelleme, geçici tablo) demektir.",
    ),
    MetricMeaning(
        "fpi_ratio",
        "Tam sayfa yazma oranı",
        "×",
        "WAL kaydı başına düşen tam sayfa görüntüsü (full page image) sayısı.",
        "Yüksekse checkpoint aralığı çok sıktır: her checkpoint sonrası bir sayfaya ilk "
        "dokunuş, sayfanın TAMAMINI WAL'a yazdırır. Çözüm sorguda değil, "
        "`checkpoint_timeout` / `max_wal_size` ayarındadır.",
    ),
    MetricMeaning(
        "plan_time_share_pct",
        "Planlama payı",
        "%",
        "Toplam sürenin, sorguyu çalıştırmak değil PLANLAMAK için harcanan kısmı.",
        "%10'u geçtiğinde sorgu çok sık yeniden planlanıyor demektir. Çözüm sorguyu "
        "hızlandırmak değil, hazırlanmış ifade (prepared statement) kullanmak ya da "
        "uygulamanın bağlantı havuzunu gözden geçirmektir.",
    ),
    MetricMeaning(
        "jit_time_share_pct",
        "JIT payı",
        "%",
        "Toplam sürenin JIT derlemesinde geçen kısmı.",
        "Kısa süren sorgularda JIT fayda değil MALİYETTİR: derleme süresi kazandırdığından "
        "uzun sürer. %15'i geçiyorsa `jit_above_cost` eşiğini yükseltmek ya da bu iş yükü "
        "için JIT'i kapatmak gerekir.",
    ),
    MetricMeaning(
        "rows_per_call",
        "Çağrı başına satır",
        "satır",
        "Sorgunun çağrı başına döndürdüğü ortalama satır sayısı.",
        "Çok yüksekse uygulama gereğinden fazla veri çekiyor olabilir (sayfalama eksikliği); "
        "sıfıra yakınsa sorgu çok sık boş dönüyor demektir ve belki hiç çalıştırılmamalıdır.",
    ),
    MetricMeaning(
        "write_blocks_per_call",
        "Çağrı başına yazılan blok",
        "blok",
        "Sorgunun kirlettiği ve diske yazdığı paylaşılan blok sayısı.",
        "Okuma sorgusu gibi görünen bir ifadenin yazma basıncının kaynağı olabilir; "
        "checkpoint yükünü ve I/O dalgalanmasını burada aramak gerekir.",
    ),
)

MEANINGS_BY_KEY: dict[str, MetricMeaning] = {m.key: m for m in METRIC_MEANINGS}

# --- Eşikler ---------------------------------------------------------------------------------
#
# Hepsi tek yerde: aynı eşiğin iki yerde farklı olması, raporun "sorun" dediğine DPA'nın
# "normal" demesi demekti.

#: Sapma ortalamanın bu katını geçerse sorgu kararsız sayılıyor.
INSTABILITY_RATIO_WARN = 1.0
#: Sürenin bu kadarı I/O beklemesiyse sorgu depolamaya bağlı.
IO_TIME_SHARE_WARN_PCT = 30.0
#: Bunun altındaki cache isabeti, çalışma kümesinin belleğe sığmadığını gösteriyor.
CACHE_HIT_WARN_PCT = 95.0
#: Planlama payı bunu geçerse sorgu çok sık yeniden planlanıyor.
PLAN_TIME_SHARE_WARN_PCT = 10.0
#: JIT payı bunu geçerse derleme maliyeti kazancı aşıyor olabilir.
JIT_TIME_SHARE_WARN_PCT = 15.0
#: Bu payın üstündeki tek bir sorgu, öncelik listesinin başına yazılıyor.
TOTAL_SHARE_HIGH_PCT = 20.0


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    value = _ratio(numerator, denominator)
    return round(value * 100, 2) if value is not None else None


def io_timing_measured(row: Any) -> bool:
    """I/O süresi GERÇEKTEN ölçüldü mü?

    `track_io_timing = off` iken sütunlar 0 gelir. Sıfırı "I/O beklemesi yok" diye sunmak
    yanlış teşhis üretir; ama sorgu gerçekten hiç diske gitmediyse de 0 olur. Ayrımı
    yapabilmek için blok SAYISINA bakılıyor: diskten blok okunmuş ama süre sıfırsa, ölçüm
    kapalı demektir.
    """
    read_blocks = float(getattr(row, "shared_blks_read", 0) or 0)
    read_time = getattr(row, "blk_read_time_ms", None)
    if read_time is None:
        return False
    if read_blocks > 0 and float(read_time) == 0.0:
        return False
    return True


def derive_metrics(row: Any, *, total_time_all_ms: float | None = None) -> dict[str, Any]:
    """Bir `SlowQuerySample` satırından türetilmiş göstergeler.

    `total_time_all_ms`: aynı pencerede ölçülen TÜM sorguların toplam süresi. Verilmezse
    "toplam etki payı" hesaplanmıyor (uydurulmuyor).
    """
    calls = float(getattr(row, "calls", 0) or 0)
    mean_ms = float(getattr(row, "mean_time_ms", 0) or 0)
    total_ms = float(getattr(row, "total_time_ms", 0) or 0)
    stddev = getattr(row, "stddev_time_ms", None)

    shared_hit = float(getattr(row, "shared_blks_hit", 0) or 0)
    shared_read = float(getattr(row, "shared_blks_read", 0) or 0)
    blocks_total = shared_hit + shared_read

    read_time = float(getattr(row, "blk_read_time_ms", 0) or 0)
    write_time = float(getattr(row, "blk_write_time_ms", 0) or 0)
    measured_io = io_timing_measured(row)

    plan_time = float(getattr(row, "total_plan_time_ms", 0) or 0)
    jit_time = float(getattr(row, "jit_time_ms", 0) or 0)

    wal_bytes = getattr(row, "wal_bytes", None)
    wal_records = float(getattr(row, "wal_records", 0) or 0)
    wal_fpi = float(getattr(row, "wal_fpi", 0) or 0)

    temp_blocks = float(getattr(row, "temp_blks_read", 0) or 0) + float(
        getattr(row, "temp_blks_written", 0) or 0
    )
    write_blocks = float(getattr(row, "shared_blks_dirtied", 0) or 0) + float(
        getattr(row, "shared_blks_written", 0) or 0
    )

    return {
        "total_share_pct": _pct(total_ms, total_time_all_ms),
        # Kararsızlık ortalamaya göre: 5 ms ortalamada 5 ms sapma ile 5 saniye ortalamada
        # 5 ms sapma bambaşka şeyler.
        "instability_ratio": (
            round(float(stddev) / mean_ms, 2) if stddev is not None and mean_ms > 0 else None
        ),
        "min_time_ms": getattr(row, "min_time_ms", None),
        "max_time_ms": getattr(row, "max_time_ms", None),
        # I/O payı yalnızca ölçüm açıkken; kapalıyken 0 DEĞİL None.
        "io_time_share_pct": _pct(read_time + write_time, total_ms) if measured_io else None,
        "io_time_measured": measured_io,
        "cache_hit_pct": _pct(shared_hit, blocks_total),
        "temp_blocks": temp_blocks or None,
        "wal_bytes_per_call": (
            round(float(wal_bytes) / calls, 1) if wal_bytes is not None and calls > 0 else None
        ),
        "fpi_ratio": round(wal_fpi / wal_records, 3) if wal_records > 0 else None,
        # Planlama payı planlama + yürütme toplamına göre: yürütmeye göre hesaplamak,
        # yürütmesi çok kısa sorgularda %1000 gibi anlamsız sayılar üretirdi.
        "plan_time_share_pct": _pct(plan_time, plan_time + total_ms),
        "jit_time_share_pct": _pct(jit_time, total_ms) if jit_time > 0 else None,
        "rows_per_call": (
            round(float(getattr(row, "rows", 0) or 0) / calls, 1) if calls > 0 else None
        ),
        "write_blocks_per_call": round(write_blocks / calls, 1) if calls > 0 else None,
    }


def flag_metrics(derived: dict[str, Any]) -> list[dict[str, str]]:
    """Eşiği aşan göstergeler: (anahtar, etiket, ne anlama geldiği, ne yapılacağı).

    Bulgu ÜRETMİYOR — yalnızca "buna bakılmalı" diyor. Bulguya dönüşme kararı rapor
    motorunun işi; burası DPA ekranının vurgusunu besliyor.
    """
    flags: list[dict[str, str]] = []

    def add(key: str, value: Any) -> None:
        meaning = MEANINGS_BY_KEY.get(key)
        if meaning is None:
            return
        flags.append(
            {
                "key": key,
                "label": meaning.label,
                "value": f"{value}{meaning.unit}" if meaning.unit != "blok" else f"{value} blok",
                "meaning": meaning.meaning,
                "when_problem": meaning.when_problem,
            }
        )

    instability = derived.get("instability_ratio")
    if instability is not None and instability >= INSTABILITY_RATIO_WARN:
        add("instability_ratio", instability)

    io_share = derived.get("io_time_share_pct")
    if io_share is not None and io_share >= IO_TIME_SHARE_WARN_PCT:
        add("io_time_share_pct", io_share)

    cache_hit = derived.get("cache_hit_pct")
    if cache_hit is not None and cache_hit < CACHE_HIT_WARN_PCT:
        add("cache_hit_pct", cache_hit)

    if derived.get("temp_blocks"):
        add("temp_blocks", derived["temp_blocks"])

    plan_share = derived.get("plan_time_share_pct")
    if plan_share is not None and plan_share >= PLAN_TIME_SHARE_WARN_PCT:
        add("plan_time_share_pct", plan_share)

    jit_share = derived.get("jit_time_share_pct")
    if jit_share is not None and jit_share >= JIT_TIME_SHARE_WARN_PCT:
        add("jit_time_share_pct", jit_share)

    total_share = derived.get("total_share_pct")
    if total_share is not None and total_share >= TOTAL_SHARE_HIGH_PCT:
        add("total_share_pct", total_share)

    return flags


def metric_dictionary() -> list[dict[str, str]]:
    """Tüm metriklerin sözlüğü — arayüzün "bu ne demek" panelini besliyor."""
    return [
        {
            "key": m.key,
            "label": m.label,
            "unit": m.unit,
            "meaning": m.meaning,
            "when_problem": m.when_problem,
        }
        for m in METRIC_MEANINGS
    ]
