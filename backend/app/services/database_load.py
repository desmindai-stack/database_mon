"""Veritabanı yükü — Average Active Sessions (Faz 25 İŞ 2).

AAS, bekleme analizinin MERKEZ metriğidir: bir zaman aralığında ortalama kaç oturumun aynı anda
iş yaptığı. Tek başına bir sayı olarak bile "sunucu ne kadar meşgul" sorusunu CPU yüzdesinden
daha doğru cevaplar (CPU yüzdesi, kilit bekleyen 40 oturumu %2 diye gösterir). Asıl gücü ise
kırılımında: AAS'i bekleme kategorisine bölünce "sistem neyi bekliyor" sorusu ölçümle
cevaplanır.

    AAS = (o aralıkta görülen aktif oturum toplamı) / (o aralıkta ALINAN örnek sayısı)

Payda ölçülür, varsayılmaz — gerekçesi models.py::ActiveSessionMinute'da.

VERİ YETERSİZSE SAYI ÜRETİLMEZ. Tek bir dakikanın örneğiyle "yükünüz 3.2" demek, kanıtsız
bulgu üretmektir; bu durumda `unavailable_reason` doldurulup seri boş dönüyor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.engines import DatabaseEngine
from app.domain.waits import (
    CATEGORY_ORDER,
    WaitCategory,
    category_label,
    category_meaning,
    is_load_bearing,
)
from app.models import ActiveSessionMinute, Instance, WaitQuerySignature, WaitSampleMinute

#: Grafikte hedeflenen nokta sayısı. Daha fazlası hem ağdan boşuna geçer hem de ekranda
#: piksel başına birden çok noktaya düşerek okunaksızlaşır.
TARGET_POINTS = 180

#: Aralık ne kadar dar olursa olsun kova en az bir dakika: veri zaten dakikalık toplanıyor,
#: daha ince kova var olmayan bir çözünürlük uydurmak olurdu.
MIN_BUCKET_SECONDS = 60

#: "Yük üreten sorgular" listesinde kaç sorgu gösterilecek.
TOP_QUERY_LIMIT = 10

#: Bir kategorinin "baskın" sayılması için gereken pay. Altındaysa tek bir suçlu yok demektir
#: ve öyle söylenir — %34'lük bir kategoriye bakıp "IO darboğazı" demek yanıltıcı olurdu.
DOMINANCE_THRESHOLD_PCT = 40.0

#: Anlamlı bir ortalama için gereken en az örnek. 1 saniyelik örneklemede 60 örnek = 1 dakika.
MIN_SAMPLES_FOR_ANALYSIS = 60


@dataclass
class LoadPoint:
    bucket_start: datetime
    total_aas: float
    blocked_aas: float
    by_category: dict[str, float]


@dataclass
class CategoryShare:
    category: str
    label: str
    meaning: str
    aas: float
    share_pct: float


@dataclass
class QueryLoad:
    queryid: str
    query: str
    aas: float
    share_pct: float
    dominant_category: str | None
    dominant_share_pct: float
    wait_profile: list[CategoryShare] = field(default_factory=list)


@dataclass
class DatabaseLoadReport:
    instance_id: int
    engine: str
    start: datetime
    end: datetime
    bucket_seconds: int
    samples_taken: int
    average_aas: float
    peak_aas: float
    blocked_aas: float
    series: list[LoadPoint] = field(default_factory=list)
    categories: list[CategoryShare] = field(default_factory=list)
    top_queries: list[QueryLoad] = field(default_factory=list)
    dominant_category: str | None = None
    dominant_share_pct: float = 0.0
    dominant_verdict: str = ""
    query_attribution_available: bool = True
    unavailable_reason: str | None = None


def choose_bucket_seconds(start: datetime, end: datetime) -> int:
    """Aralığa göre kova genişliği: nokta sayısı TARGET_POINTS civarında kalsın."""
    span = max((end - start).total_seconds(), MIN_BUCKET_SECONDS)
    raw = span / TARGET_POINTS
    minutes = max(1, int(raw // 60) + (1 if raw % 60 else 0))
    return minutes * 60


def _epoch_bucket(column, bucket_seconds: int):
    """`minute` sütununu kova başlangıcının epoch saniyesine çevirir.

    Toplama VERİTABANINDA yapılıyor: 7 günlük bir aralıkta ham satır sayısı yüz binleri
    bulabiliyor ve hepsini Python'a çekmek, Faz 21'de `slow_query_samples` yüzünden yaşanan
    502'nin aynısını davet ederdi. Epoch ifadesi lehçeye göre değişiyor (SQLite'ta
    `strftime`, PostgreSQL'de `extract`) — tek fark bu, gerisi ortak.
    """
    if settings.database_url.startswith("sqlite"):
        epoch = cast(func.strftime("%s", column), BigInteger)
    else:
        epoch = cast(func.extract("epoch", column), BigInteger)
    return (epoch / bucket_seconds) * bucket_seconds


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def build_database_load(
    session: AsyncSession,
    instance: Instance,
    *,
    hours: int = 1,
    start: datetime | None = None,
    end: datetime | None = None,
) -> DatabaseLoadReport:
    now = datetime.now(UTC)
    window_end = _as_utc(end) if end else now
    window_start = _as_utc(start) if start else window_end - timedelta(hours=hours)
    bucket_seconds = choose_bucket_seconds(window_start, window_end)

    report = DatabaseLoadReport(
        instance_id=instance.id,
        engine=instance.engine,
        start=window_start,
        end=window_end,
        bucket_seconds=bucket_seconds,
        samples_taken=0,
        average_aas=0.0,
        peak_aas=0.0,
        blocked_aas=0.0,
    )

    if instance.engine == str(DatabaseEngine.MONGODB):
        report.unavailable_reason = (
            "MongoDB'de bekleme (wait event) sözlüğünün karşılığı yok — currentOp bekleme TİPİ "
            "vermiyor. Bu yüzden veritabanı yükü kırılımı MongoDB için üretilmiyor."
        )
        return report
    if not settings.wait_sampling_enabled:
        report.unavailable_reason = (
            "Bekleme örnekleyicisi kapalı (WAIT_SAMPLING_ENABLED=false). Veritabanı yükü "
            "grafiği örneklemeye dayanıyor; açmadan veri birikmez."
        )
        return report

    bucket_col = _epoch_bucket(ActiveSessionMinute.minute, bucket_seconds)
    totals_rows = (
        await session.execute(
            select(
                bucket_col.label("bucket"),
                func.sum(ActiveSessionMinute.samples_taken).label("samples"),
                func.sum(ActiveSessionMinute.active_sessions_sampled).label("active"),
                func.sum(ActiveSessionMinute.blocked_sessions_sampled).label("blocked"),
            )
            .where(
                ActiveSessionMinute.instance_id == instance.id,
                ActiveSessionMinute.minute >= window_start,
                ActiveSessionMinute.minute <= window_end,
            )
            .group_by(bucket_col)
            .order_by(bucket_col)
        )
    ).all()

    total_samples = sum(int(r.samples or 0) for r in totals_rows)
    report.samples_taken = total_samples
    if total_samples == 0:
        report.unavailable_reason = (
            "Bu aralıkta hiç bekleme örneği yok. Örnekleyici yalnızca worker sürecinde "
            "(RUN_MODE=worker/all) çalışıyor; yeni eklenen bir instance'ta ilk verinin "
            "birikmesi birkaç dakika sürer."
        )
        return report
    if total_samples < MIN_SAMPLES_FOR_ANALYSIS:
        report.unavailable_reason = (
            f"Yalnızca {total_samples} örnek var; anlamlı bir ortalama için en az "
            f"{MIN_SAMPLES_FOR_ANALYSIS} gerekiyor (1 sn örneklemede ~1 dakika). Bu kadar az "
            "örnekten yük ortalaması üretmek, ölçüm gibi görünen bir tahmin olurdu."
        )
        return report

    breakdown_bucket = _epoch_bucket(WaitSampleMinute.minute, bucket_seconds)
    breakdown_rows = (
        await session.execute(
            select(
                breakdown_bucket.label("bucket"),
                WaitSampleMinute.wait_category,
                func.sum(WaitSampleMinute.sample_count).label("samples"),
            )
            .where(
                WaitSampleMinute.instance_id == instance.id,
                WaitSampleMinute.minute >= window_start,
                WaitSampleMinute.minute <= window_end,
            )
            .group_by(breakdown_bucket, WaitSampleMinute.wait_category)
        )
    ).all()

    samples_by_bucket = {int(r.bucket): int(r.samples or 0) for r in totals_rows}
    blocked_by_bucket = {int(r.bucket): int(r.blocked or 0) for r in totals_rows}
    per_bucket: dict[int, dict[str, int]] = {}
    for row in breakdown_rows:
        if not is_load_bearing(row.wait_category):
            # Arka plan boşta beklemesi yük değildir; saymak grafiğe yalancı bir taban ekler.
            continue
        per_bucket.setdefault(int(row.bucket), {})[row.wait_category] = int(row.samples or 0)

    window_category_samples: dict[str, int] = {}
    peak = 0.0
    for bucket, samples in sorted(samples_by_bucket.items()):
        if samples <= 0:
            continue
        categories = per_bucket.get(bucket, {})
        by_category = {cat: count / samples for cat, count in categories.items()}
        total_aas = sum(by_category.values())
        peak = max(peak, total_aas)
        for cat, count in categories.items():
            window_category_samples[cat] = window_category_samples.get(cat, 0) + count
        report.series.append(
            LoadPoint(
                bucket_start=datetime.fromtimestamp(bucket, tz=UTC),
                total_aas=round(total_aas, 3),
                blocked_aas=round(blocked_by_bucket.get(bucket, 0) / samples, 3),
                by_category={cat: round(value, 3) for cat, value in by_category.items()},
            )
        )

    total_category_samples = sum(window_category_samples.values())
    report.average_aas = round(total_category_samples / total_samples, 3)
    report.peak_aas = round(peak, 3)
    report.blocked_aas = round(sum(blocked_by_bucket.values()) / total_samples, 3)
    report.categories = _category_shares(window_category_samples, total_samples)

    if report.categories:
        top = report.categories[0]
        report.dominant_share_pct = top.share_pct
        if top.share_pct >= DOMINANCE_THRESHOLD_PCT:
            report.dominant_category = top.category
            report.dominant_verdict = (
                f"Yükün %{top.share_pct:.0f}'i {top.label.lower()} kaynaklı. {top.meaning}"
            )
        else:
            report.dominant_verdict = (
                f"Tek bir baskın kaynak yok — en yüksek pay %{top.share_pct:.0f} ile "
                f"{top.label.lower()}. Yük birden çok kaynağa dağılmış durumda; tek bir "
                "değişiklikle toparlanması beklenmemeli."
            )

    report.top_queries = await _top_queries(session, instance.id, window_start, window_end, total_samples)
    if not report.top_queries and total_category_samples > 0:
        report.query_attribution_available = False

    return report


def _category_shares(samples_by_category: dict[str, int], total_samples: int) -> list[CategoryShare]:
    total = sum(samples_by_category.values())
    if total == 0:
        return []
    shares = [
        CategoryShare(
            category=cat,
            label=category_label(cat),
            meaning=category_meaning(cat),
            aas=round(count / total_samples, 3),
            share_pct=round(count * 100.0 / total, 1),
        )
        for cat, count in samples_by_category.items()
    ]
    shares.sort(key=lambda s: (-s.aas, _category_rank(s.category)))
    return shares


def _category_rank(category: str) -> int:
    try:
        return CATEGORY_ORDER.index(WaitCategory(category))
    except ValueError:
        return len(CATEGORY_ORDER)


async def _top_queries(
    session: AsyncSession,
    instance_id: int,
    start: datetime,
    end: datetime,
    total_samples: int,
) -> list[QueryLoad]:
    """Aralıkta EN ÇOK YÜK ÜRETEN sorgular ve her birinin bekleme profili.

    "En yavaş sorgu" ile "en çok yük üreten sorgu" aynı şey değildir: 5 saniye süren ama günde
    iki kez çalışan bir sorgu, 20 ms süren ama saniyede 300 kez çalışan bir sorgunun yanında
    hiçbir şeydir. AAS ikincisini öne çıkarır — DPA'nın asıl katkısı budur.
    """
    rows = (
        await session.execute(
            select(
                WaitSampleMinute.queryid,
                WaitSampleMinute.wait_category,
                func.sum(WaitSampleMinute.sample_count).label("samples"),
            )
            .where(
                WaitSampleMinute.instance_id == instance_id,
                WaitSampleMinute.minute >= start,
                WaitSampleMinute.minute <= end,
                WaitSampleMinute.queryid != "",
            )
            .group_by(WaitSampleMinute.queryid, WaitSampleMinute.wait_category)
        )
    ).all()
    if not rows:
        return []

    by_query: dict[str, dict[str, int]] = {}
    for row in rows:
        if not is_load_bearing(row.wait_category):
            continue
        by_query.setdefault(row.queryid, {})[row.wait_category] = int(row.samples or 0)
    if not by_query:
        return []

    grand_total = sum(sum(cats.values()) for cats in by_query.values())
    ranked = sorted(by_query.items(), key=lambda kv: -sum(kv[1].values()))[:TOP_QUERY_LIMIT]

    texts = dict(
        (
            await session.execute(
                select(WaitQuerySignature.queryid, WaitQuerySignature.query_text).where(
                    WaitQuerySignature.instance_id == instance_id,
                    WaitQuerySignature.queryid.in_([q for q, _ in ranked]),
                )
            )
        ).all()
    )

    result: list[QueryLoad] = []
    for queryid, categories in ranked:
        query_samples = sum(categories.values())
        profile = _category_shares(categories, total_samples)
        dominant = profile[0] if profile else None
        result.append(
            QueryLoad(
                queryid=queryid,
                # Metin sözlükte yoksa uydurulmuyor: kullanıcı en azından kimliği görüp
                # pg_stat_statements'ta arayabilsin.
                query=texts.get(queryid) or f"(sorgu metni kaydedilmemiş — queryid {queryid})",
                aas=round(query_samples / total_samples, 3),
                share_pct=round(query_samples * 100.0 / grand_total, 1),
                dominant_category=dominant.category if dominant else None,
                dominant_share_pct=dominant.share_pct if dominant else 0.0,
                wait_profile=profile,
            )
        )
    return result


def report_to_dict(report: DatabaseLoadReport) -> dict[str, Any]:
    return {
        "instance_id": report.instance_id,
        "engine": report.engine,
        "start": report.start,
        "end": report.end,
        "bucket_seconds": report.bucket_seconds,
        "samples_taken": report.samples_taken,
        "average_aas": report.average_aas,
        "peak_aas": report.peak_aas,
        "blocked_aas": report.blocked_aas,
        "series": [
            {
                "bucket_start": p.bucket_start,
                "total_aas": p.total_aas,
                "blocked_aas": p.blocked_aas,
                "by_category": p.by_category,
            }
            for p in report.series
        ],
        "categories": [_share_to_dict(c) for c in report.categories],
        "top_queries": [
            {
                "queryid": q.queryid,
                "query": q.query,
                "aas": q.aas,
                "share_pct": q.share_pct,
                "dominant_category": q.dominant_category,
                "dominant_share_pct": q.dominant_share_pct,
                "wait_profile": [_share_to_dict(c) for c in q.wait_profile],
            }
            for q in report.top_queries
        ],
        "dominant_category": report.dominant_category,
        "dominant_share_pct": report.dominant_share_pct,
        "dominant_verdict": report.dominant_verdict,
        "query_attribution_available": report.query_attribution_available,
        "unavailable_reason": report.unavailable_reason,
    }


def _share_to_dict(share: CategoryShare) -> dict[str, Any]:
    return {
        "category": share.category,
        "label": share.label,
        "meaning": share.meaning,
        "aas": share.aas,
        "share_pct": share.share_pct,
    }
