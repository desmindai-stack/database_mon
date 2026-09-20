"""Kesinti tespitinin TEK gerçeklik kaynağı (Faz 28 İŞ 3b).

Kesinti pencereleri eskiden yalnızca rapor bölümünün içinde hesaplanıyordu. SLA takibi de
aynı sayıya ihtiyaç duyunca iki seçenek vardı: hesabı kopyalamak ya da tek yere taşımak.
Kopyalamak, raporun "%99.95" derken SLA ekranının "%99.7" demesi demekti — CLAUDE.md'deki
"aynı veriyi gösteren yerler tek kaynaktan beslensin" kuralının tam olarak uyardığı durum.

ÖNEMLİ SINIR (rapordan taşındı, aynen geçerli): bu ölçüm "dbace bu instance'tan veri
toplayamadı" demektir — veritabanının gerçekten kapalı olduğunu KANITLAMAZ. dbace worker'ı
durmuş, ağ kopmuş ya da kimlik bilgisi geçersiz olmuş da olabilir.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Instance, MetricSample
from app.services.collection import effective_collect_interval

#: Toplama aralığının kaç katı boşluk "kesinti" sayılır. 1 kaçırılan döngü ağ gecikmesi veya
#: yavaş bir sorgu yüzünden olabilir; 3 katı artık gerçek bir kopukluktur.
OUTAGE_GAP_MULTIPLIER = 3

#: Bu süreden kısa boşluklar raporlanmaz — 15 sn'lik toplamada 45 sn'lik bir gecikme kesinti
#: değil gürültüdür.
MIN_OUTAGE_SECONDS = 60.0


def as_utc(value: datetime) -> datetime:
    """SQLite naive, Postgres aware datetime döndürür — karşılaştırmalar iki motorda da
    çalışsın diye tek noktada UTC'ye sabitleniyor."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def gap_threshold(instance: Instance) -> float:
    return max(effective_collect_interval(instance) * OUTAGE_GAP_MULTIPLIER, MIN_OUTAGE_SECONDS)


async def outages_for_instance(
    session: AsyncSession, instance: Instance, period_start: datetime, period_end: datetime
) -> list[dict]:
    """Toplama boşluklarından türetilen kesinti pencereleri.

    Sonda kalan boşluk da dahil (Faz 28 İŞ 2): eskiden yalnızca İKİ ÖLÇÜM ARASINDAKİ
    boşluklara bakılıyordu ve bir düğüm üç saat önce düşüp bir daha gelmediyse rapor hiç
    kesinti göstermiyordu — en kötü durum tam da görünmez olan durumdu.
    """
    threshold = gap_threshold(instance)
    in_period = [
        MetricSample.instance_id == instance.id,
        MetricSample.collected_at >= period_start,
        MetricSample.collected_at <= period_end,
    ]
    last = (
        await session.execute(select(MetricSample.collected_at).where(*in_period)
                              .order_by(MetricSample.collected_at.desc()).limit(1))
    ).scalar_one_or_none()
    if last is None:
        return []

    outages: list[dict] = []
    for previous, current in await _gaps(session, in_period, threshold):
        previous, current = as_utc(previous), as_utc(current)
        gap = (current - previous).total_seconds()
        if gap >= threshold:
            outages.append(
                {
                    "start": previous.isoformat(),
                    "end": current.isoformat(),
                    "seconds": round(gap, 1),
                    "ongoing": False,
                }
            )

    trailing = (period_end - as_utc(last)).total_seconds()
    if trailing >= threshold:
        outages.append(
            {
                "start": as_utc(last).isoformat(),
                "end": period_end.isoformat(),
                "seconds": round(trailing, 1),
                "ongoing": True,
            }
        )
    return outages


async def _gaps(session: AsyncSession, in_period: list, threshold: float) -> list[tuple[datetime, datetime]]:
    """Ardışık iki ölçüm arasındaki boşluklar — yalnızca eşiği aşanlar döner (Faz 31 Commit 9, egress).

    PostgreSQL'de fark ve eşik SQL'de (`LAG`): dönemdeki her ölçüm zamanı çekilmiyordu değil, çekiliyordu —
    günlük raporda instance başına ~5760 satır. SQLite'ta (yalnızca yerel geliştirme/test; meta veritabanı
    egress'i yok) zaman aritmetiği taşınabilir değil: aynı karşılaştırma Python'da. İki yolun aynı sonucu
    verdiğini canlı test ölçüyor (tests/test_meta_egress_live_postgres.py).
    """
    previous = func.lag(MetricSample.collected_at, type_=MetricSample.collected_at.type).over(
        order_by=[MetricSample.collected_at, MetricSample.id])
    pairs = select(previous.label("previous"), MetricSample.collected_at.label("current")).where(*in_period).subquery()
    if session.bind.dialect.name == "postgresql":
        gap = func.extract("epoch", pairs.c.current - pairs.c.previous)
        rows = await session.execute(
            select(pairs.c.previous, pairs.c.current).where(pairs.c.previous.is_not(None), gap >= threshold)
            .order_by(pairs.c.current)
        )
        return [(r.previous, r.current) for r in rows]
    rows = await session.execute(select(pairs.c.previous, pairs.c.current).where(pairs.c.previous.is_not(None))
                                 .order_by(pairs.c.current))
    return [(r.previous, r.current) for r in rows]


async def first_sample_at(session: AsyncSession, instance: Instance) -> datetime | None:
    """İlk ölçüm zamanı — "izlenebildiği süre" hesabı için.

    Dönemin tamamı için değil İZLENEBİLDİĞİ süre için yüzde hesaplanıyor: instance dönemin
    ortasında eklendiyse ondan önceki zamanı "kesinti" saymak yanlış olurdu.
    """
    value = (
        await session.execute(
            select(MetricSample.collected_at)
            .where(MetricSample.instance_id == instance.id)
            .order_by(MetricSample.collected_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return as_utc(value) if value is not None else None
