from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Instance, MetricSample
from app.schemas import MetricSampleOut

#: Grafik için yeterli nokta sayısı; uzun aralıklarda örnekler SQL'de eşit aralıkla seyreltiliyor.
DEFAULT_MAX_POINTS = 720

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/{instance_id}", response_model=list[MetricSampleOut])
async def get_metrics(
    instance_id: int,
    hours: int = Query(default=1, ge=1, le=168),
    start: datetime | None = Query(
        default=None, description="Özel aralık başlangıcı (ISO-8601). Verilirse `hours` yok sayılır."
    ),
    end: datetime | None = Query(default=None, description="Özel aralık bitişi (ISO-8601)."),
    max_points: int = Query(
        default=DEFAULT_MAX_POINTS, ge=10, le=5000,
        description="Kaç örnek döneceğinin üst sınırı (Faz 31 Commit 9). Aralıkta daha çok örnek varsa eşit aralıkla "
        "seyreltilir; ilk ve son örnek her zaman dahil (bu yüzden sonuç en fazla bir örnek daha uzun olabilir).",
    ),
    db: AsyncSession = Depends(get_db),
) -> list[MetricSample]:
    """Faz 16-B İŞ 3: hazır aralıkların (1/6/24 saat, 7 gün) yanında özel aralık desteği.

    `start`/`end` verilmezse eski davranış aynen korunuyor (son `hours` saat).
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

    conditions = [MetricSample.instance_id == instance_id]
    if start or end:
        # Naive datetime gelirse UTC varsayıyoruz: frontend her zaman ISO/UTC gönderiyor, ama
        # elle çağıran bir istemci saat dilimsiz gönderirse karşılaştırma patlamasın.
        if start:
            conditions.append(MetricSample.collected_at >= _as_utc(start))
        if end:
            conditions.append(MetricSample.collected_at <= _as_utc(end))
    else:
        conditions.append(MetricSample.collected_at >= datetime.now(UTC) - timedelta(hours=hours))

    # Faz 31 Commit 9 (egress): 24 saatlik aralık 15 sn aralıkla 5760 tam satır demekti (her biri 40+
    # anahtarlık metrics_json) ve ekran 15 saniyede bir yeniliyor. Seyreltme SQL'de: her `adım`ıncı örnek.
    numbered = (
        select(
            MetricSample.id,
            func.row_number().over(order_by=[MetricSample.collected_at, MetricSample.id]).label("rn"),
            func.count().over().label("total"),
        )
        .where(*conditions)
        .subquery()
    )
    # TAM BÖLME (`//`): SQLAlchemy 2.0'da `/` gerçek bölme — 1.33'lük bir adım her satırı elerdi.
    step = (numbered.c.total + max_points - 1) // max_points
    kept = select(numbered.c.id).where(
        or_((numbered.c.rn - 1) % step == 0, numbered.c.rn == numbered.c.total)
    )
    result = await db.execute(
        select(MetricSample).where(MetricSample.id.in_(kept)).order_by(MetricSample.collected_at.asc())
    )
    return [MetricSampleOut.from_orm_sample(row) for row in result.scalars().all()]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.get("/{instance_id}/latest", response_model=MetricSampleOut | None)
async def get_latest_metrics(instance_id: int, db: AsyncSession = Depends(get_db)) -> MetricSample | None:
    """Son metrik ornegi; henuz hic toplanmamissa `null`.

    Faz 19 IS 1 — API DEGISIKLIGI: eskiden ornek yoksa 404 "No metrics collected yet"
    doniyordu. Instance'in KENDISI duruyorken 404 donmek yanlis: istemci bunu "instance
    silinmis" durumundan ayirt edemiyor, sonucta yeni eklenmis (henuz veri gelmemis) bir
    instance icin "bulunamadi" ekrani cikiyordu. Bos alt koleksiyon bir hata degil.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

    result = await db.execute(
        select(MetricSample)
        .where(MetricSample.instance_id == instance_id)
        .order_by(MetricSample.collected_at.desc())
        .limit(1)
    )
    sample = result.scalar_one_or_none()
    if not sample:
        return None
    return MetricSampleOut.from_orm_sample(sample)
