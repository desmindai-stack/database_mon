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

from sqlalchemy import and_, case, false, func, literal, not_, or_, select, true, update
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

#: İmza deseninin etiketi — sınıflandırma sonucu bu etiketse satırın KİMDEN geldiğine bakılıyor.
DBACE_MARKER_LABEL = "dbace'in kendi sorgusu"

#: İmzalı metin ama dbace DIŞI rolden çağrı: filtrelenmiyor, işaretleniyor.
MARKER_CONFLICT_NOTE = (
    "Sorgu metni dbace imzası (/* dbace */) taşıyor ama çağrılar dbace'in izleme rolünden DEĞİL, "
    "başka bir rolden geliyor. Uygulama yükü olduğu için gizlenmedi. Olası sebep: dbace'in "
    "bağlandığı rol ile aynı adı taşıyan bir istemci ya da imzayı kopyalayan bir araç."
)

_SYSTEM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bpg_catalog\.", "pg_catalog"),
    (r"\bpg_stat_[a-z_]+", "pg_stat_* görünümleri"),
    (r"\bpg_walfile_name(_offset)?\s*\(", "pg_walfile_name (WAL izleme)"),
    (r"\bpg_current_wal_", "WAL konum fonksiyonları"),
    (r"\bpg_last_wal_", "WAL konum fonksiyonları"),
    (r"\binformation_schema\.", "information_schema"),
    # Faz 31 İŞ 1a: şemasız yazılan katalog tabloları. `pg_catalog.` öneki olmadan da
    # PostgreSQL bunları pg_catalog'dan çözer; eski listede yoktu ve `SELECT ... FROM pg_class`
    # sorguları index önerisine girip "public.pg_class bulunamadı" hatası üretiyordu.
    # FROM/JOIN'e bağlı: `pg_classification` gibi bir kolon adı ya da dizgi içindeki
    # "from pg_class" metni eşleşmemeli — ikisi de testle sabit.
    (
        r"\b(?:FROM|JOIN)\s+(?:pg_class|pg_namespace|pg_attribute|pg_index|pg_indexes|pg_tables"
        r"|pg_proc|pg_type|pg_locks|pg_roles|pg_constraint|pg_views|pg_sequences|pg_extension"
        r"|pg_description|pg_depend|pg_am|pg_inherits|pg_trigger|pg_statistic|pg_stats)\b",
        "sistem kataloğu tablosu",
    ),
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
    # Faz 31: izlenen sunucuya giden HER sorgu bu imzayı taşıyor (collectors/query_marker.py).
    # İmza queryid'ye girmiyor, sorgu ŞEKLİNİ işaretliyor — ILERLEME.md Faz 31 Commit 1.
    (r"/\*\s*dbace\s*\*/", DBACE_MARKER_LABEL),
    (r"--\s*ext:", "dbace ön koşul denetimi"),
    (r"\bhypopg_", "dbace index danışmanı (hypopg)"),
)

_COMPILED_SYSTEM_PATTERNS = tuple((re.compile(p, re.IGNORECASE), label) for p, label in _SYSTEM_PATTERNS)


def classify_system_query(query: str) -> str | None:
    """Sistem/platform sorgusuysa NEDEN öyle sayıldığını döndürür, değilse None.

    Sebebi de döndürmesi bilinçli: arayüzde "sistem sorgusu" etiketi tek başına yeterli değil,
    kullanıcı hangi kurala takıldığını görebilmeli (yanlış sınıflandırmayı fark etmek için).
    """
    # Dizgi sabitleri desen aramasından ÖNCE boşaltılıyor (Faz 31): `WHERE note LIKE
    # '%pg_stat_%'` bir uygulama sorgusudur, sistem sorgusu değil. Yorumlar KORUNUYOR —
    # dbace'in imzası (`/* dbace */`) ve `-- ext:` işareti yorumun içinde.
    text = _STRING_LITERAL.sub("''", query or "")
    for pattern, label in _COMPILED_SYSTEM_PATTERNS:
        if pattern.search(text):
            return label
    return None


#: Tek tırnaklı dizgi sabiti; `''` kaçışı dahil.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


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
    # İmzalı metin, dbace dışı rolden çağrı — filtrelenmedi, gösteriliyor (Faz 31 Commit 4).
    marker_conflict: bool = False

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
    # Faz 31 Commit 9: filtreleri geçen TOPLAM kalem (sayfalamadan önce) — SQL `count(*)`.
    visible_count: int = 0


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
    # Faz 31 Commit 4: dbace'in kendi satırı ve iç içe çalıştırma AYRI grup. Aynı queryid'yi
    # taşısalar da ayrı pg_stat_statements sayaçları; birleştirildiklerinde fark hesabı iki
    # seriyi karıştırıyordu ve imzalı dbace satırının metni uygulama yükünü "sistem sorgusu"
    # diye gizleyebiliyordu. Uygulamanın üst düzey satırı eski anahtarı koruyor (rapor derin
    # bağlantıları bozulmasın).
    suffix = (":dbace" if row.from_monitoring_role else "") + (":nested" if row.toplevel is False else "")
    text_key = query_fingerprint(row.query) + suffix
    if row.queryid:
        key = f"id:{row.queryid}{suffix}"
        # Aynı metin için daha önce bir kimlik belirlendiyse ona bağlan.
        return text_to_key.setdefault(text_key, key)
    return text_to_key.setdefault(text_key, text_key)


#: Tuning içgörüsünün saydığı ve "İlgili sekmeye git" bağlantısının açtığı liste görünümü (Faz 31
#: Commit 7). Arayüzün sunduğu en büyük "ilk N" değeri; sayı bu görünümün dışına taşamaz.
INSIGHT_LIST_SORT = "mean"
INSIGHT_LIST_LIMIT = 20
#: İçgörünün "yavaş" saydığı ortalama süre eşiği (ms).
SLOW_MEAN_MS = 50.0


async def default_slow_query_selection(
    session: AsyncSession,
    instance_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    sort: str = "total",
    limit: int = 20,
    offset: int = 0,
    include_system: bool | None = None,
) -> SlowQuerySelection:
    """Yavaş sorgu LİSTESİ, Tuning içgörüsü ve teşhis panelinin TEK kaynağı (Faz 31 Commit 7).

    Aynı pencere (varsayılan son `DEFAULT_WINDOW_HOURS` saat), aynı sistem/imza filtresi ve aynı
    eşikler (yönetim ayarı). Eskiden içgörü ve teşhis son anlık görüntünün HAM satırlarını
    okuyordu: sistem sorguları, dbace'in kendi imzalı sorguları ve kümülatif ortalama dahil —
    "2 yavaş sorgu" deniyor, listede 1 görünüyordu (gerçek veride ölçüldü; canlıda 17).
    """
    from datetime import timedelta

    from app.services.noise_settings import get_noise_settings

    noise = await get_noise_settings(session)
    window_end = end or datetime.now(UTC)
    window_start = start or (window_end - timedelta(hours=DEFAULT_WINDOW_HOURS))
    return await select_slow_queries(
        session,
        instance_id,
        start=window_start,
        end=window_end,
        sort=sort,
        limit=limit,
        offset=offset,
        include_system=noise["show_system_queries"] if include_system is None else include_system,
        min_total_ms=noise["list_min_total_ms"],
        min_calls=noise["list_min_calls"],
    )


#: Liste yollarında metnin okunacağı en fazla kalem (sayfa). Metin ayrıntıda tam okunuyor.
MAX_PAGE_ITEMS = 200


def _selection_groups(instance_id: int, start: datetime | None, end: datetime | None):
    """Penceredeki her sorgu grubunun İLK ve SON örneği — METİNSİZ, SQL'de (Faz 31 Commit 9).

    Eskiden penceredeki her satır tam kolon (metin dahil, satır başına ~840 bayt) çekilip Python'da
    gruplanıyordu: 24 saatte instance başına ~4-14 bin satır, arayüz 15 saniyede bir yeniliyor — canlıda
    Supabase egress kotasının 17 katı. Gruplama kuralları aynı (`_merge_key`):

    - metin kimliği = `query_hash` + rol/iç içe son eki; metnin grubu, o metnin PENCEREDEKİ İLK
      örneğinin anahtarı (`id:<queryid><sonek>`; queryid yoksa metin kimliği),
    - ilk/son örnek toplama zamanına (eşitlikte id'ye) göre.
    """
    s = SlowQuerySample
    conditions = [s.instance_id == instance_id]
    if start:
        conditions.append(s.collected_at >= _as_utc(start))
    if end:
        conditions.append(s.collected_at <= _as_utc(end))
    suffix = case((s.from_monitoring_role == true(), literal(":dbace")), else_=literal("")) + case(
        (s.toplevel == false(), literal(":nested")), else_=literal("")
    )
    window = (
        select(
            s.id, s.collected_at, s.queryid, s.query_hash, s.query_class, s.calls, s.total_time_ms,
            s.mean_time_ms, s.rows,
            case((s.from_monitoring_role == true(), 1), (s.from_monitoring_role == false(), 0)).label("fmr"),
            suffix.label("suffix"),
        )
        .where(*conditions)
        .cte("sq_window")
    )
    first_qid = func.first_value(window.c.queryid).over(
        partition_by=[window.c.query_hash, window.c.suffix], order_by=[window.c.collected_at, window.c.id]
    )
    keyed = select(window, first_qid.label("first_qid")).cte("sq_keyed")
    group_key = case(
        (and_(keyed.c.first_qid.is_not(None), keyed.c.first_qid != ""),
         literal("id:") + keyed.c.first_qid + keyed.c.suffix),
        else_=keyed.c.query_hash + keyed.c.suffix,
    )
    ranked_src = select(keyed, group_key.label("gkey")).cte("sq_grouped")
    ranked = select(
        ranked_src,
        func.row_number().over(partition_by=ranked_src.c.gkey,
                               order_by=[ranked_src.c.collected_at, ranked_src.c.id]).label("rn_first"),
        func.row_number().over(partition_by=ranked_src.c.gkey,
                               order_by=[ranked_src.c.collected_at.desc(), ranked_src.c.id.desc()]).label("rn_last"),
        func.min(ranked_src.c.id).over(partition_by=ranked_src.c.gkey).label("first_seen"),
    ).cte("sq_ranked")

    def at(rn, column):
        return func.max(case((rn == 1, column)))

    groups = (
        select(
            ranked.c.gkey.label("key"),
            func.count().label("n"),
            func.min(ranked.c.first_seen).label("first_seen"),
            at(ranked.c.rn_last, ranked.c.id).label("last_id"),
            at(ranked.c.rn_last, ranked.c.queryid).label("queryid"),
            at(ranked.c.rn_last, ranked.c.calls).label("last_calls"),
            at(ranked.c.rn_last, ranked.c.total_time_ms).label("last_total"),
            at(ranked.c.rn_last, ranked.c.mean_time_ms).label("last_mean"),
            at(ranked.c.rn_last, ranked.c.rows).label("last_rows"),
            at(ranked.c.rn_last, ranked.c.query_class).label("query_class"),
            at(ranked.c.rn_last, ranked.c.query_hash).label("query_hash"),
            at(ranked.c.rn_last, ranked.c.fmr).label("fmr"),
            at(ranked.c.rn_first, ranked.c.calls).label("first_calls"),
            at(ranked.c.rn_first, ranked.c.total_time_ms).label("first_total"),
        )
        .group_by(ranked.c.gkey)
        .cte("sq_groups")
    )
    cycles = select(func.count(func.distinct(window.c.collected_at))).scalar_subquery()
    return groups, cycles


def _computed(groups, cycles, *, include_system: bool, min_total_ms: float, min_calls: int,
              pending: dict[str, str] | None = None):
    """Fark, sınıf ve eşik kararları SQL ifadesi olarak — liste ve SAYI aynı ifadelerden (Commit 7)."""
    g = groups.c
    query_class = (
        func.coalesce(g.query_class, case(pending, value=g.query_hash, else_=literal("")))
        if pending else g.query_class
    )
    cumulative = or_(cycles <= 1, g.n == 1)
    reset = or_(g.last_total < g.first_total, g.last_calls < g.first_calls)
    total = case((or_(cumulative, reset), g.last_total), else_=g.last_total - g.first_total)
    calls = case((or_(cumulative, reset), g.last_calls), else_=g.last_calls - g.first_calls)
    mean = case((calls > 0, total / calls), else_=g.last_mean)
    marker_conflict = and_(query_class == DBACE_MARKER_LABEL, g.fmr == 0)
    is_system = and_(query_class.is_not(None), query_class != "", not_(marker_conflict))
    visible_system = true() if include_system else not_(is_system)
    significant = and_(total >= min_total_ms, calls >= min_calls)
    computed = select(
        g.key, g.queryid, g.last_id, g.last_rows, query_class.label("query_class"), g.n, g.first_seen,
        total.label("total"), calls.label("calls"), mean.label("mean"),
        marker_conflict.label("marker_conflict"), is_system.label("is_system"),
        case((total <= 0, "zero"), (not_(visible_system), "system"), (not_(significant), "insignificant"),
             else_="visible").label("verdict"),
    ).cte("sq_computed")
    return computed


_ORDER = {"total": "total", "mean": "mean", "calls": "calls"}


async def _pending_classes(session: AsyncSession, groups) -> dict[str, str]:
    """Migration'dan önce yazılmış satırların sınıfı yok (NULL): metin METİN BAŞINA bir kez okunup bellekte
    sınıflandırılıyor ve SQL'e ifade olarak veriliyor. İstek yolu YAZMIYOR; kalıcı sınıfı arka plan işi
    (`backfill_query_classes`) toplu UPDATE ile yazıyor."""
    rows = (
        await session.execute(select(groups.c.query_hash, groups.c.last_id).where(groups.c.query_class.is_(None)))
    ).all()
    if not rows:
        return {}
    texts = dict((
        await session.execute(select(SlowQuerySample.id, SlowQuerySample.query)
                              .where(SlowQuerySample.id.in_([r.last_id for r in rows][:MAX_PAGE_ITEMS])))
    ).all())
    return {r.query_hash: classify_system_query(texts.get(r.last_id) or "") or "" for r in rows if r.last_id in texts}


async def backfill_query_classes(session: AsyncSession, *, batch: int = 500) -> int:
    """Sınıfı olmayan satırlar için: parmak izi başına TEK metin okunur, sınıf o parmak izli bütün satırlara
    TEK UPDATE ile yazılır (satır satır değil). Döndürdüğü: sınıflandırılan farklı metin sayısı.

    Parmak izi olmayan satırlar (PostgreSQL'de migration dolduruyor; yalnızca SQLite geliştirme veritabanı) önce
    parmak izi alıyor — yine farklı metin başına tek UPDATE."""
    while True:
        rows = (
            await session.execute(select(SlowQuerySample.id, SlowQuerySample.query)
                                  .where(SlowQuerySample.query_hash.is_(None)).limit(batch))
        ).all()
        if not rows:
            break
        by_hash: dict[str, list[int]] = {}
        for row_id, text in rows:
            by_hash.setdefault(query_fingerprint(text or ""), []).append(row_id)
        for query_hash, ids in by_hash.items():
            await session.execute(update(SlowQuerySample).where(SlowQuerySample.id.in_(ids)).values(query_hash=query_hash))
        await session.commit()
    done = 0
    while True:
        pending = (
            await session.execute(
                select(SlowQuerySample.query_hash, func.min(SlowQuerySample.id).label("sample_id"))
                .where(SlowQuerySample.query_class.is_(None), SlowQuerySample.query_hash.is_not(None))
                .group_by(SlowQuerySample.query_hash)
                .limit(batch)
            )
        ).all()
        if not pending:
            return done
        texts = dict((
            await session.execute(select(SlowQuerySample.id, SlowQuerySample.query)
                                  .where(SlowQuerySample.id.in_([r.sample_id for r in pending])))
        ).all())
        for query_hash, sample_id in pending:
            await session.execute(
                update(SlowQuerySample)
                .where(SlowQuerySample.query_hash == query_hash, SlowQuerySample.query_class.is_(None))
                .values(query_class=classify_system_query(texts.get(sample_id) or "") or "")
            )
        await session.commit()
        done += len(pending)


async def _load_samples(session: AsyncSession, ids: list[int]) -> dict[int, SlowQuerySample]:
    """Sayfadaki kalemlerin SON örnekleri — tam satır yalnızca gösterilen kalemler için (≤ MAX_PAGE_ITEMS)."""
    if not ids:
        return {}
    if len(ids) > MAX_PAGE_ITEMS:
        raise ValueError(f"sayfa {MAX_PAGE_ITEMS} kalemi aşamaz ({len(ids)})")
    rows = (await session.execute(select(SlowQuerySample).where(SlowQuerySample.id.in_(ids)))).scalars().all()
    return {row.id: row for row in rows}


async def select_slow_queries(
    session: AsyncSession,
    instance_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    sort: str = "total",
    limit: int = 20,
    offset: int = 0,
    include_system: bool = False,
    min_total_ms: float = DEFAULT_MIN_TOTAL_MS,
    min_calls: int = DEFAULT_MIN_CALLS,
    keys: list[str] | None = None,
    with_samples: bool = True,
) -> SlowQuerySelection:
    """Pencere içindeki en sorunlu sorgular — rapor ve DPA'nın ORTAK kaynağı.

    Sıralama her zaman pencere içindeki değişime göre yapılır. Pencerede tek bir toplama
    döngüsü varsa fark alınamaz; bu durumda kümülatif değerler `mode="snapshot"` ile
    döndürülür (yeni eklenmiş bir instance'ta listeyi boş bırakmak yerine, ne gösterildiğini
    dürüstçe söylemek).

    Faz 31 Commit 9: gruplama, fark, sınıf filtresi, eşik, sayım, sıralama ve LIMIT SQL'de; meta
    veritabanından yalnızca sayfadaki kalemler döner. `with_samples=False` metni ve ham satırı hiç
    okumaz (içgörü, sayım). `keys` verilirse yalnızca o gruplar (rapordaki önceki dönem karşılaştırması).
    """
    limit = max(0, min(int(limit), MAX_PAGE_ITEMS))
    groups, cycles = _selection_groups(instance_id, start, end)
    computed = _computed(groups, cycles, include_system=include_system, min_total_ms=min_total_ms,
                         min_calls=min_calls, pending=await _pending_classes(session, groups))
    c = computed.c

    counts = (
        await session.execute(
            select(
                func.count().filter(c.verdict == "system").label("system"),
                func.count().filter(c.verdict == "insignificant").label("insignificant"),
                func.count().filter(c.verdict == "visible").label("visible"),
                func.count().label("groups"),
                cycles.label("cycles"),
            )
        )
    ).one()
    if not counts.groups:
        return SlowQuerySelection([], "delta", start, end)
    mode = "delta" if (counts.cycles or 0) > 1 else "snapshot"

    order_col = getattr(c, _ORDER.get(sort, "total"))
    page_query = select(computed).where(c.verdict == "visible")
    if keys is not None:
        page_query = select(computed).where(c.verdict != "zero", c.key.in_(keys or [""]))
    page = (
        await session.execute(page_query.order_by(order_col.desc(), c.first_seen).limit(limit).offset(offset))
    ).all()

    samples = await _load_samples(session, [r.last_id for r in page]) if with_samples else {}
    entries: list[SlowQueryEntry] = []
    for r in page:
        total = round(float(r.total or 0), 2)
        calls = int(r.calls or 0)
        sample = samples.get(r.last_id)
        is_system = bool(r.is_system)
        entries.append(
            SlowQueryEntry(
                key=r.key,
                queryid=r.queryid,
                query=sample.query if sample is not None else "",
                calls=calls,
                total_time_ms=total,
                mean_time_ms=round(total / calls, 2) if calls > 0 else float(r.mean or 0),
                rows=int(r.last_rows or 0),
                sample=sample,
                system_reason=(r.query_class or None) if is_system else None,
                sample_count=int(r.n or 0),
                marker_conflict=bool(r.marker_conflict),
            )
        )
    return SlowQuerySelection(
        entries=entries,
        mode=mode,
        window_start=start,
        window_end=end,
        filtered_system=int(counts.system or 0),
        filtered_insignificant=int(counts.insignificant or 0),
        visible_count=int(counts.visible or 0),
    )


async def count_slow_in_list_view(
    session: AsyncSession,
    instance_id: int,
    *,
    threshold_ms: float,
    sort: str = "mean",
    limit: int = 20,
) -> tuple[int, SlowQuerySelection]:
    """Tuning "N yavaş ortalama süreli sorgu" sayısı — LİSTE GÖRÜNÜMÜYLE AYNI CTE'den `count(*)`
    (Faz 31 Commit 7 sözleşmesi, Commit 9'da SQL'e taşındı). Seçimin kendisi de metinsiz dönüyor:
    içgörü sorgu metni kullanmıyor."""
    from app.services.noise_settings import get_noise_settings

    from datetime import timedelta

    noise = await get_noise_settings(session)
    end = datetime.now(UTC)
    start = end - timedelta(hours=DEFAULT_WINDOW_HOURS)
    groups, cycles = _selection_groups(instance_id, start, end)
    computed = _computed(groups, cycles, include_system=noise["show_system_queries"],
                         min_total_ms=noise["list_min_total_ms"], min_calls=noise["list_min_calls"],
                         pending=await _pending_classes(session, groups))
    c = computed.c
    view = (
        select(c.mean).where(c.verdict == "visible")
        .order_by(getattr(c, _ORDER.get(sort, "mean")).desc(), c.first_seen).limit(limit).subquery()
    )
    count = (await session.execute(select(func.count()).select_from(view).where(view.c.mean >= threshold_ms))).scalar_one()
    selection = await select_slow_queries(
        session, instance_id, start=start, end=end, sort=sort, limit=limit,
        include_system=noise["show_system_queries"], min_total_ms=noise["list_min_total_ms"],
        min_calls=noise["list_min_calls"], with_samples=False,
    )
    return int(count), selection


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
    Eşikler ve sistem filtresi BURADA uygulanmaz: kullanıcı belirli bir sorguyu istedi. Faz 31 Commit 9:
    arama SQL'de (anahtar ya da queryid), yalnızca bulunan kalem okunuyor.
    """
    groups, cycles = _selection_groups(instance_id, start, end)
    computed = _computed(groups, cycles, include_system=True, min_total_ms=0.0, min_calls=0,
                         pending=await _pending_classes(session, groups))
    c = computed.c
    condition = c.key == key if key is not None else c.queryid == queryid
    row = (
        await session.execute(select(c.key).where(c.verdict != "zero", condition).order_by(c.first_seen).limit(1))
    ).first()
    if row is None:
        return None
    selection = await select_slow_queries(
        session, instance_id, start=start, end=end, limit=1, include_system=True, min_total_ms=0.0,
        min_calls=0, keys=[row.key],
    )
    return selection.entries[0] if selection.entries else None
