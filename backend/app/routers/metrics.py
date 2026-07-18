from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Instance, MetricSample, SlowQuerySample
from app.schemas import MetricSampleOut, SlowQueryOut

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/{instance_id}", response_model=list[MetricSampleOut])
async def get_metrics(
    instance_id: int,
    hours: int = Query(default=1, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
) -> list[MetricSample]:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    since = datetime.now(UTC) - timedelta(hours=hours)
    result = await db.execute(
        select(MetricSample)
        .where(MetricSample.instance_id == instance_id, MetricSample.collected_at >= since)
        .order_by(MetricSample.collected_at.asc())
    )
    return [MetricSampleOut.from_orm_sample(row) for row in result.scalars().all()]


@router.get("/{instance_id}/latest", response_model=MetricSampleOut)
async def get_latest_metrics(instance_id: int, db: AsyncSession = Depends(get_db)) -> MetricSample:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    result = await db.execute(
        select(MetricSample)
        .where(MetricSample.instance_id == instance_id)
        .order_by(MetricSample.collected_at.desc())
        .limit(1)
    )
    sample = result.scalar_one_or_none()
    if not sample:
        raise HTTPException(status_code=404, detail="No metrics collected yet")
    return MetricSampleOut.from_orm_sample(sample)
