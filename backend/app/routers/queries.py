from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Instance, SlowQuerySample
from app.schemas import SlowQueryOut

router = APIRouter(prefix="/queries", tags=["queries"])


@router.get("/{instance_id}", response_model=list[SlowQueryOut])
async def get_slow_queries(
    instance_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[SlowQuerySample]:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    subq = (
        select(SlowQuerySample.collected_at)
        .where(SlowQuerySample.instance_id == instance_id)
        .order_by(SlowQuerySample.collected_at.desc())
        .limit(1)
    )
    latest_at = (await db.execute(subq)).scalar_one_or_none()
    if not latest_at:
        return []

    result = await db.execute(
        select(SlowQuerySample)
        .where(
            SlowQuerySample.instance_id == instance_id,
            SlowQuerySample.collected_at == latest_at,
        )
        .order_by(SlowQuerySample.mean_time_ms.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
