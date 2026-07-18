from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.database import get_db
from app.models import Instance, SlowQuerySample
from app.schemas import IndexAdviceOut, IndexAdviceRequest, SlowQueryOut
from app.services.credentials import decrypt_secret
from app.services.index_advisor import PostgreSQLIndexAdvisor

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


@router.post("/{instance_id}/advice", response_model=list[IndexAdviceOut])
async def advise_indexes(
    instance_id: int,
    body: IndexAdviceRequest,
    db: AsyncSession = Depends(get_db),
) -> list[IndexAdviceOut]:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="Index advice is only available for PostgreSQL")

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    advisor = PostgreSQLIndexAdvisor(target)
    recommendations = await advisor.advise(body.query)
    return [
        IndexAdviceOut(
            table_name=r.table_name,
            schema_name=r.schema_name,
            columns=r.columns,
            index_ddl=r.index_ddl,
            reason=r.reason,
            estimated_improvement_pct=r.estimated_improvement_pct,
            has_hypopg_estimate=r.has_hypopg_estimate,
            before_cost=r.before_cost,
            after_cost=r.after_cost,
            existing_indexes=r.existing_indexes,
        )
        for r in recommendations
    ]
