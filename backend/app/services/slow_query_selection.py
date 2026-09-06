"""Yavaş sorgu seçiminin TEK kaynağı (Faz 18 İŞ 1).

**Bildirilen hata:** Rapor "en pahalı sorgular" bölümünde bir sorgu gösteriyor, aynı sorgu
DPA'da hiç görünmüyordu. Öneri "DPA'da EXPLAIN'e bakın" diyordu ama tıklanınca sorgu orada yok.

**Kök neden — iki ayrı seçim mantığı vardı:**

* Rapor: dönemin TAMAMI üzerinde kümülatif sayaç farkı (window delta).
* DPA (varsayılan görünüm): yalnızca EN SON toplama döngüsünün anlık görüntüsü.

Collector her döngüde pg_stat_statements'ın ilk 20 satırını saklıyor. Dönem içinde bir kez
öne çıkıp sonra listeden düşen bir sorgu, dönem farkında görünüyor ama son döngüde yok — yani
rapor onu gösterirken DPA gösteremiyordu. Aynı veriden iki farklı gerçek.

Bu modül seçimi tek yere topluyor; rapor da DPA da buradan besleniyor, dolayısıyla ikisinin
ayrışması yapısal olarak mümkün değil.

**İkinci kök neden — kimlik parçalanması.** Gruplama `queryid or query` ile yapılıyordu.
pg_stat_statements, ayrıcalıksız rollerde bazı satırların `queryid` alanını NULL döndürür
(bkz. Faz 16-B İŞ 1); aynı sorgunun bir örneği queryid'li, diğeri queryid'siz gelince tek
sorgu İKİ ayrı gruba bölünüyor ve raporda aynı sorgu iki kez listeleniyordu. Artık kimlik,
sorgu metninden türeyen kararlı bir parmak iziyle birleştiriliyor.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SlowQuerySample

# Bir sorgunun listeye girebilmesi için pencerede harcaması gereken en az toplam süre.
# 1 ms'lik eski taban gürültüyü elemiyordu (bkz. Faz 18 İŞ 2 — ayarlanabilir eşikler).
DEFAULT_MIN_TOTAL_MS = 100.0
# Tek çağrılık bir sorgudan trend çıkarılamaz; liste için de anlamlı bir sinyal değil.
DEFAULT_MIN_CALLS = 1
# Aralık verilmediğinde bakılan varsayılan pencere. Eskiden "yalnızca son toplama döngüsü"
# vardı; rapor dönemin tamamına baktığı için ikisi ayrışıyordu (Faz 18 İŞ 1).
DEFAULT_WINDOW_HOURS = 24


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def normalize_query(query: str) -> str:
    """Kimlik karşılaştırması için sorgu metnini sadeleştirir (boşluk/büyük-küçük harf)."""
    return " ".join((query or "").split()).lower()


def query_fingerprint(query: str) -> str:
    """Sorgu metninden kararlı kimlik — `queryid` yokken ya da NULL geldiğinde kullanılır."""
    return "q:" + hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()[:24]


# --- Sistem / platform sorgusu sınıflandırması ------------------------------------------
#
# Bu sorgular veritabanının kendi iç işleyişine ya da izleme araçlarına ait. DBA'nın
# optimize edebileceği bir şey değiller; raporda "en pahalı sorgu" olarak çıkmaları gürültü.
# Varsayılan olarak filtreleniyorlar, ayardan açılabiliyorlar (Faz 18 İŞ 2).

_SYSTEM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bpg_catalog\.", "pg_catalog"),
    (r"\bpg_stat_[a-z_]+", "pg_stat_* görünümleri"),
    (r"\bpg_walfile_name(_offset)?\s*\(", "pg_walfile_name (WAL izleme)"),
    (r"\bpg_current_wal_", "WAL konum fonksiyonları"),
    (r"\bpg_last_wal_", "WAL konum fonksiyonları"),
    (r"\binformation_schema\.", "information_schema"),
    (r"\bpg_database\b", "pg_database katalogu"),
    (r"\bpg_settings\b", "pg_settings katalogu"),
    (r"\bpg_replication_slots\b", "replikasyon katalogu"),
    (r"\bpg_is_in_recovery\s*\(", "replikasyon durumu"),
    (r"\bpg_ls_waldir\s*\(", "WAL dizini listeleme"),
    # Platform iç sorguları
    (r"\bsupabase_admin\b|\bsupabase_functions\b|\brealtime\.", "Supabase iç sorgusu"),
    (r"\brdsadmin\b|\brds_", "RDS iç sorgusu"),
    (r"\bcloudsqladmin\b", "Cloud SQL iç sorgusu"),
    (r"\bazure_maintenance\b", "Azure iç sorgusu"),
    # dbace'in KENDİ toplama sorguları — kendi gürültüsünü raporlamamalı.
    (r"--\s*ext:", "dbace ön koşul denetimi"),
    (r"\bhypopg_", "dbace index danışmanı (hypopg)"),
)

_COMPILED_SYSTEM_PATTERNS = tuple((re.compile(p, re.IGNORECASE), label) for p, label in _SYSTEM_PATTERNS)


def classify_system_query(query: str) -> str | None:
    """Sistem/platform sorgusuysa NEDEN öyle sayıldığını döndürür, değilse None.

    Sebebi de döndürmesi bilinçli: arayüzde "sistem sorgusu" etiketi tek başına yeterli değil,
    kullanıcı hangi kurala takıldığını görebilmeli (yanlış sınıflandırmayı fark etmek için).
    """
    text = query or ""
    for pattern, label in _COMPILED_SYSTEM_PATTERNS:
        if pattern.search(text):
            return label
    return None


@dataclass
class SlowQueryEntry:
    """Bir sorgunun pencere içindeki DEĞİŞİMİ + kimliği ve sınıflandırması."""

    key: str
    queryid: str | None
    query: str
    calls: int
    total_time_ms: float
    mean_time_ms: float
    rows: int
    # Tanı (diagnose_query) ve EXPLAIN için gereken ham son örnek.
    sample: SlowQuerySample
    system_reason: str | None = None
    # Penceredeki örnek sayısı; 1 ise fark hesaplanamamıştır (bkz. `mode`).
    sample_count: int = 0

    @property
    def is_system(self) -> bool:
        return self.system_reason is not None


@dataclass
class SlowQuerySelection:
    entries: list[SlowQueryEntry]
    # "delta"  — pencerede en az iki toplama döngüsü var, fark alındı (normal durum)
    # "snapshot" — pencerede tek döngü var, fark alınamadı; kümülatif değerler gösteriliyor
    mode: str
    window_start: datetime | None
    window_end: datetime | None
    # Eşiklerin/sistem filtresinin elediği sorgu sayısı — arayüz "N sorgu filtrelendi" diyebilsin.
    filtered_system: int = 0
    filtered_insignificant: int = 0


SORT_KEYS = {
    "total": lambda e: e.total_time_ms,
    "mean": lambda e: e.mean_time_ms,
    "calls": lambda e: e.calls,
}


def _merge_key(row: SlowQuerySample, text_to_key: dict[str, str]) -> str:
    """Bir örneğin ait olduğu kimlik.

    `queryid` varsa o esastır; ama aynı sorgu metni daha önce queryid'siz görülmüşse iki grup
    birleştirilir. Böylece pg_stat_statements'ın bazı satırlarda queryid'yi NULL döndürmesi
    tek sorguyu ikiye bölmez.
    """
    text_key = query_fingerprint(row.query)
    if row.queryid:
        key = f"id:{row.queryid}"
        # Aynı metin için daha önce bir kimlik belirlendiyse ona bağlan.
        return text_to_key.setdefault(text_key, key)
    return text_to_key.setdefault(text_key, text_key)


async def select_slow_queries(
    session: AsyncSession,
    instance_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    sort: str = "total",
    limit: int = 20,
    include_system: bool = False,
    min_total_ms: float = DEFAULT_MIN_TOTAL_MS,
    min_calls: int = DEFAULT_MIN_CALLS,
) -> SlowQuerySelection:
    """Pencere içindeki en sorunlu sorgular — rapor ve DPA'nın ORTAK kaynağı.

    Sıralama her zaman pencere içindeki değişime göre yapılır. Pencerede tek bir toplama
    döngüsü varsa fark alınamaz; bu durumda kümülatif değerler `mode="snapshot"` ile
    döndürülür (yeni eklenmiş bir instance'ta listeyi boş bırakmak yerine, ne gösterildiğini
    dürüstçe söylemek).
    """
    conditions = [SlowQuerySample.instance_id == instance_id]
    if start:
        conditions.append(SlowQuerySample.collected_at >= _as_utc(start))
    if end:
        conditions.append(SlowQuerySample.collected_at <= _as_utc(end))

    rows = list(
        (
            await session.execute(
                select(SlowQuerySample).where(*conditions).order_by(SlowQuerySample.collected_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return SlowQuerySelection([], "delta", start, end)

    distinct_cycles = {row.collected_at for row in rows}
    mode = "delta" if len(distinct_cycles) > 1 else "snapshot"

    text_to_key: dict[str, str] = {}
    grouped: dict[str, list[SlowQuerySample]] = {}
    for row in rows:
        grouped.setdefault(_merge_key(row, text_to_key), []).append(row)

    entries: list[SlowQueryEntry] = []
    filtered_system = 0
    filtered_insignificant = 0

    for key, group in grouped.items():
        group.sort(key=lambda r: r.collected_at)
        first, last = group[0], group[-1]

        if mode == "snapshot" or len(group) == 1:
            # Fark alınamıyor: penceredeki tek örneğin kümülatif değeri.
            total = float(last.total_time_ms or 0)
            calls = int(last.calls or 0)
        else:
            reset = last.total_time_ms < first.total_time_ms or last.calls < first.calls
            total = float(last.total_time_ms if reset else last.total_time_ms - first.total_time_ms)
            calls = int(last.calls if reset else last.calls - first.calls)

        if total <= 0:
            continue

        entry = SlowQueryEntry(
            key=key,
            queryid=last.queryid,
            query=last.query,
            calls=calls,
            total_time_ms=round(total, 2),
            mean_time_ms=round(total / calls, 2) if calls > 0 else float(last.mean_time_ms or 0),
            rows=int(last.rows or 0),
            sample=last,
            system_reason=classify_system_query(last.query),
            sample_count=len(group),
        )

        if entry.is_system and not include_system:
            filtered_system += 1
            continue
        if entry.total_time_ms < min_total_ms or entry.calls < min_calls:
            filtered_insignificant += 1
            continue
        entries.append(entry)

    entries.sort(key=SORT_KEYS.get(sort, SORT_KEYS["total"]), reverse=True)
    return SlowQuerySelection(
        entries=entries[:limit],
        mode=mode,
        window_start=start,
        window_end=end,
        filtered_system=filtered_system,
        filtered_insignificant=filtered_insignificant,
    )


async def find_slow_query(
    session: AsyncSession,
    instance_id: int,
    *,
    queryid: str | None = None,
    key: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> SlowQueryEntry | None:
    """Belirli bir sorguyu pencerede bulur — rapor derin bağlantısının hedefi.

    Rapor bir sorgudan bahsediyorsa o sorgunun DPA'da bulunabilir olması gerekiyor; bu
    fonksiyon o güvenceyi test edilebilir hale getiriyor (bkz. tests/test_report_dpa_consistency.py).
    Eşikler ve sistem filtresi BURADA uygulanmaz: kullanıcı belirli bir sorguyu istedi.
    """
    selection = await select_slow_queries(
        session,
        instance_id,
        start=start,
        end=end,
        limit=10_000,
        include_system=True,
        min_total_ms=0.0,
        min_calls=0,
    )
    for entry in selection.entries:
        if key is not None and entry.key == key:
            return entry
        if queryid is not None and entry.queryid == queryid:
            return entry
    return None
