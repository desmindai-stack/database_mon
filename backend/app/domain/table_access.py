"""Tablo erişim kalıpları: sıralı tarama, cache isabeti, HOT güncelleme (Faz 29 İŞ 2b).

Mevcut şema sağlığı "tablo şişmiş mi, vacuum gecikmiş mi, index kullanılmıyor mu" sorularını
cevaplıyordu. Eksik olan, tabloya NASIL ERİŞİLDİĞİ:

* **Sıralı tarama baskın mı** — index önerisi sorgu bazında çalışıyor; bu ise tablo bazında
  "buraya hiç index'le girilmiyor" sinyali veriyor ve sorgu listesinde görünmeyen
  (pg_stat_statements'a düşmeyen, ör. uygulama dışı) erişimleri de kapsıyor.
* **Cache isabeti** — tablo ve index bloklarının kaçta kaçı bellekten karşılanıyor.
* **HOT güncelleme oranı** — güncellemelerin kaçta kaçı index'lere dokunmadan yapılabiliyor.
  Düşükse her güncelleme tüm index'leri de yazıyor demektir; bu, yazma yükünün gizli
  kaynağıdır ve sorgu listesine hiç yansımaz.
* **Analiz gecikmesi** — istatistikler son ANALYZE'dan beri kaç satır değişti. Zaman yerine
  DEĞİŞİM HACMİ bakmak doğru ölçü: az yazılan bir tabloda 3 gün eski istatistik sorun değil,
  çok yazılan bir tabloda 1 saat eski istatistik sorundur.

## Gürültü üretmemek için eşikler

En kolay hata, küçük tablolardaki sıralı taramayı sorun saymak olurdu. **1000 satırlık bir
tabloda sıralı tarama DOĞRU davranıştır**; planlayıcı index'i bilerek kullanmaz, çünkü tüm
tabloyu okumak daha ucuzdur. Bu yüzden eşik "kaç kez tarandı" değil, **tarama başına kaç
satır okundu** üzerinden kuruluyor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Sıralı tarama başına bu kadar satır okunuyorsa tarama "büyük" sayılıyor.
#: Küçük tablolardaki sıralı tarama planlayıcının DOĞRU tercihidir; eşiği satır sayısına
#: bağlamak, tam da o doğru tercihleri bulgu diye göstermeyi engelliyor.
SEQ_SCAN_ROWS_WARN = 10_000

#: Toplam taramaların bu kadarı sıralıysa tabloya "index'le girilmiyor" demektir.
SEQ_SCAN_SHARE_WARN = 0.5

#: Tablo/index cache isabeti bunun altındaysa çalışma kümesi belleğe sığmıyor.
CACHE_HIT_WARN_PCT = 95.0
#: Cache isabeti yorumlamak için en az bu kadar blok okunmuş olmalı — 10 bloklu bir tabloda
#: %50 isabet istatistiksel gürültüdür.
CACHE_MIN_BLOCKS = 10_000

#: HOT güncelleme oranı bunun altındaysa her güncelleme index'leri de yazıyor.
HOT_UPDATE_RATIO_WARN = 0.5
#: HOT oranını yorumlamak için en az bu kadar güncelleme olmalı.
HOT_MIN_UPDATES = 10_000

#: Son ANALYZE'dan beri canlı satırların bu oranı kadar değişiklik olduysa istatistikler eski.
STALE_STATS_RATIO_WARN = 0.2
#: Çok küçük tablolarda oran hızla büyür; anlamlı olması için asgari değişiklik.
STALE_STATS_MIN_ROWS = 10_000


@dataclass(frozen=True)
class AccessSignal:
    """Tablo erişiminde tespit edilen bir durum."""

    key: str
    severity: str  # critical | warning | info
    title: str
    #: Bu sayı NE ÖLÇÜYOR.
    meaning: str
    #: NE ZAMAN sorun.
    when_problem: str
    #: Bu tablodaki somut değer.
    evidence: dict[str, Any]


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    value = _ratio(numerator, denominator)
    return round(value * 100, 2) if value is not None else None


def derive_table_access(row: dict[str, Any]) -> dict[str, Any]:
    """Ham `pg_stat_user_tables` + `pg_statio_user_tables` satırından oranlar."""
    seq_scan = float(row.get("seq_scan") or 0)
    seq_tup_read = float(row.get("seq_tup_read") or 0)
    idx_scan = float(row.get("idx_scan") or 0)
    total_scans = seq_scan + idx_scan

    heap_hit = float(row.get("heap_blks_hit") or 0)
    heap_read = float(row.get("heap_blks_read") or 0)
    idx_hit = float(row.get("idx_blks_hit") or 0)
    idx_read = float(row.get("idx_blks_read") or 0)

    updates = float(row.get("n_tup_upd") or 0)
    hot_updates = float(row.get("n_tup_hot_upd") or 0)

    live = float(row.get("n_live_tup") or 0)
    modified = float(row.get("n_mod_since_analyze") or 0)

    return {
        "seq_scan_share_pct": _pct(seq_scan, total_scans),
        # Tarama BAŞINA satır: "kaç kez tarandı" değil "her taramada ne kadar okundu".
        "rows_per_seq_scan": round(seq_tup_read / seq_scan, 1) if seq_scan > 0 else None,
        "heap_cache_hit_pct": _pct(heap_hit, heap_hit + heap_read),
        "index_cache_hit_pct": _pct(idx_hit, idx_hit + idx_read),
        "heap_blocks_total": heap_hit + heap_read,
        "hot_update_pct": _pct(hot_updates, updates),
        "updates": updates,
        # Değişim hacmi / canlı satır: zaman yerine DEĞİŞİM bakmak doğru ölçü.
        "modified_since_analyze_pct": _pct(modified, live) if live > 0 else None,
        "modified_since_analyze": modified,
    }


def analyze_table_access(row: dict[str, Any]) -> list[AccessSignal]:
    """Bir tablonun erişim kalıbındaki sinyaller.

    Sinyal ÜRETMEK bulgu üretmek değil: rapor motoru bunları bulguya çevirip çevirmeyeceğine
    kendi karar veriyor. Buradaki iş, eşiği aşan durumu ve NEDEN önemli olduğunu söylemek.
    """
    derived = derive_table_access(row)
    signals: list[AccessSignal] = []
    table = f"{row.get('schema_name')}.{row.get('table_name')}"

    rows_per_seq = derived["rows_per_seq_scan"]
    seq_share = derived["seq_scan_share_pct"]
    if (
        rows_per_seq is not None
        and rows_per_seq >= SEQ_SCAN_ROWS_WARN
        and seq_share is not None
        and seq_share >= SEQ_SCAN_SHARE_WARN * 100
    ):
        signals.append(
            AccessSignal(
                key="seq_scan_dominant",
                severity="warning",
                title=f"{table}: sıralı tarama baskın",
                meaning=(
                    "Tabloya yapılan erişimlerin çoğu index kullanmadan, tablonun tamamı "
                    "okunarak yapılıyor ve her taramada ortalama "
                    f"{rows_per_seq:,.0f} satır okunuyor.".replace(",", ".")
                ),
                when_problem=(
                    "Küçük tablolarda sıralı tarama DOĞRU davranıştır — planlayıcı index'i "
                    "bilerek kullanmaz. Sorun, her taramada on binlerce satır okunuyorsa "
                    "başlar: o zaman her sorgu tablonun tamamını diskten/cache'ten geçirir ve "
                    "tablo büyüdükçe yavaşlama doğrusal olarak artar. Uygun index, bu maliyeti "
                    "sabit seviyeye indirir."
                ),
                evidence={
                    "metric": "seq_scan_rows_per_scan",
                    "value": rows_per_seq,
                    "threshold": SEQ_SCAN_ROWS_WARN,
                    "seq_scan_share_pct": seq_share,
                    "seq_scan": row.get("seq_scan"),
                    "idx_scan": row.get("idx_scan"),
                },
            )
        )

    heap_hit_pct = derived["heap_cache_hit_pct"]
    if (
        heap_hit_pct is not None
        and heap_hit_pct < CACHE_HIT_WARN_PCT
        and derived["heap_blocks_total"] >= CACHE_MIN_BLOCKS
    ):
        signals.append(
            AccessSignal(
                key="low_table_cache_hit",
                severity="warning",
                title=f"{table}: tablo cache isabeti düşük",
                meaning=(
                    f"Tablo bloklarının yalnızca %{heap_hit_pct}'i bellekten karşılandı; "
                    "gerisi diskten okundu."
                ),
                when_problem=(
                    "%95'in altı, bu tablonun sık erişilen kısmının `shared_buffers` içine "
                    "sığmadığını gösterir. Tek seferlik büyük bir tarama da böyle görünebilir; "
                    "kalıcıysa ya bellek yetersizdir ya da sorgular gereğinden fazla veri "
                    "okuyordur (index eksikliği)."
                ),
                evidence={
                    "metric": "heap_cache_hit_pct",
                    "value": heap_hit_pct,
                    "threshold": CACHE_HIT_WARN_PCT,
                    "blocks": derived["heap_blocks_total"],
                },
            )
        )

    hot_pct = derived["hot_update_pct"]
    if hot_pct is not None and hot_pct < HOT_UPDATE_RATIO_WARN * 100 and derived["updates"] >= HOT_MIN_UPDATES:
        signals.append(
            AccessSignal(
                key="low_hot_update_ratio",
                severity="warning",
                title=f"{table}: HOT güncelleme oranı düşük",
                meaning=(
                    f"Güncellemelerin yalnızca %{hot_pct}'i HOT (heap-only tuple) olarak "
                    "yapılabildi; kalanı tablodaki TÜM index'leri de yeniden yazdı."
                ),
                when_problem=(
                    "Bu, yazma yükünün sorgu listesinde GÖRÜNMEYEN kaynağıdır: bir satır "
                    "güncellemesi, tablonun index sayısı kadar ek yazma üretir. İki sebebi "
                    "olur — sayfalarda HOT için boş yer kalmaması (`fillfactor`) ya da "
                    "güncellenen kolonun index'li olması. Kullanılmayan index'leri silmek "
                    "ve fillfactor'ü düşürmek doğrudan yazma yükünü azaltır."
                ),
                evidence={
                    "metric": "hot_update_pct",
                    "value": hot_pct,
                    "threshold": HOT_UPDATE_RATIO_WARN * 100,
                    "updates": derived["updates"],
                },
            )
        )

    modified_pct = derived["modified_since_analyze_pct"]
    if (
        modified_pct is not None
        and modified_pct >= STALE_STATS_RATIO_WARN * 100
        and derived["modified_since_analyze"] >= STALE_STATS_MIN_ROWS
    ):
        signals.append(
            AccessSignal(
                key="stale_statistics",
                severity="warning",
                title=f"{table}: planlayıcı istatistikleri eskimiş",
                meaning=(
                    f"Son ANALYZE'dan beri canlı satır sayısının %{modified_pct}'i kadar "
                    "değişiklik yapıldı."
                ),
                when_problem=(
                    "Planlayıcı kararlarını istatistiklere göre veriyor. Eskimiş istatistik, "
                    "satır tahminlerinin sapmasına ve yanlış plan seçimine yol açar — sorgu "
                    "bir gün hızlı, ertesi gün kat kat yavaş çalışır. Zaman yerine DEĞİŞİM "
                    "HACMİ ölçülüyor: az yazılan bir tabloda 3 gün eski istatistik sorun "
                    "değil, çok yazılan bir tabloda 1 saat eski istatistik sorundur."
                ),
                evidence={
                    "metric": "modified_since_analyze_pct",
                    "value": modified_pct,
                    "threshold": STALE_STATS_RATIO_WARN * 100,
                    "modified_rows": derived["modified_since_analyze"],
                },
            )
        )

    return signals
