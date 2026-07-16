from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.pg_collector import PostgresCollector, PostgresTarget
from app.database import get_db
from app.models import AlertEvent, AlertRule, Instance, MetricSample, SlowQuerySample
from app.schemas import (
    AlertEventOut,
    AlertRuleCreate,
    AlertRuleOut,
    ConnectionTestResult,
    InstanceCreate,
    InstanceOut,
    InstanceSummary,
    InstanceUpdate,
    MetricSampleOut,
    SlowQueryOut,
)

router = APIRouter(prefix="/instances", tags=["instances"])


@router.get("", response_model=list[InstanceOut])
async def list_instances(db: AsyncSession = Depends(get_db)) -> list[Instance]:
    result = await db.execute(select(Instance).order_by(Instance.name))
    return list(result.scalars().all())


@router.post("", response_model=InstanceOut, status_code=status.HTTP_201_CREATED)
async def create_instance(payload: InstanceCreate, db: AsyncSession = Depends(get_db)) -> Instance:
    existing = await db.execute(select(Instance).where(Instance.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Instance name already exists")

    instance = Instance(**payload.model_dump())
    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    return instance


@router.get("/summary", response_model=list[InstanceSummary])
async def list_summaries(db: AsyncSession = Depends(get_db)) -> list[InstanceSummary]:
    instances = (await db.execute(select(Instance).order_by(Instance.name))).scalars().all()
    summaries: list[InstanceSummary] = []

    for instance in instances:
        latest = (
            await db.execute(
                select(MetricSample)
                .where(MetricSample.instance_id == instance.id)
                .order_by(MetricSample.collected_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        firing = (
            await db.execute(
                select(func.count())
                .select_from(AlertEvent)
                .where(
                    AlertEvent.instance_id == instance.id,
                    AlertEvent.resolved_at.is_(None),
                )
            )
        ).scalar_one()

        status_label = "healthy"
        if not instance.enabled:
            status_label = "disabled"
        elif latest is None:
            status_label = "pending"
        elif firing:
            status_label = "alerting"
        elif latest.active_connections >= latest.max_connections * 0.9:
            status_label = "warning"

        summaries.append(
            InstanceSummary(
                instance=InstanceOut.model_validate(instance),
                latest_metrics=MetricSampleOut.model_validate(latest) if latest else None,
                status=status_label,
                alerts_firing=int(firing or 0),
            )
        )
    return summaries


@router.get("/{instance_id}", response_model=InstanceOut)
async def get_instance(instance_id: int, db: AsyncSession = Depends(get_db)) -> Instance:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance


@router.patch("/{instance_id}", response_model=InstanceOut)
async def update_instance(
    instance_id: int, payload: InstanceUpdate, db: AsyncSession = Depends(get_db)
) -> Instance:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(instance, key, value)
    await db.commit()
    await db.refresh(instance)
    return instance


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instance(instance_id: int, db: AsyncSession = Depends(get_db)) -> None:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    await db.delete(instance)
    await db.commit()


@router.post("/test", response_model=ConnectionTestResult)
async def test_connection(payload: InstanceCreate) -> ConnectionTestResult:
    collector = PostgresCollector(PostgresTarget(**payload.model_dump()))
    ok, message, details = await collector.test_connection()
    return ConnectionTestResult(ok=ok, message=message, details=details)


@router.post("/{instance_id}/test", response_model=ConnectionTestResult)
async def test_existing_instance(instance_id: int, db: AsyncSession = Depends(get_db)) -> ConnectionTestResult:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    collector = PostgresCollector(
        PostgresTarget(
            host=instance.host,
            port=instance.port,
            database=instance.database,
            username=instance.username,
            password=instance.password,
        )
    )
    ok, message, details = await collector.test_connection()
    return ConnectionTestResult(ok=ok, message=message, details=details)
