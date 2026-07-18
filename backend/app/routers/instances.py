from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.collectors.registry import get_collector
from app.database import get_db
from app.domain.engines import DatabaseEngine
from app.domain.metrics import CANONICAL_METRICS, metrics_for_engine
from app.models import AlertEvent, Instance, MetricSample, PredictionInsight
from app.schemas import (
    ConnectionTestResult,
    InstanceCreate,
    InstanceOut,
    InstanceSummary,
    InstanceUpdate,
    MetricDefinitionOut,
    MetricSampleOut,
)
from app.services.credentials import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/instances", tags=["instances"])


def _connection_target(payload: InstanceCreate) -> ConnectionTarget:
    return ConnectionTarget(
        host=payload.host,
        port=payload.resolved_port(),
        database=payload.database,
        username=payload.username,
        password=payload.password,
        options=payload.options,
    )


@router.get("/catalog/metrics", response_model=list[MetricDefinitionOut])
async def metric_catalog(engine: DatabaseEngine | None = None) -> list[MetricDefinitionOut]:
    defs = metrics_for_engine(engine) if engine else list(CANONICAL_METRICS)
    return [
        MetricDefinitionOut(
            key=m.key,
            display_name=m.display_name,
            unit=m.unit,
            category=m.category,
            engines=[e.value for e in m.engines],
            description=m.description,
        )
        for m in defs
    ]


@router.get("", response_model=list[InstanceOut])
async def list_instances(db: AsyncSession = Depends(get_db)) -> list[Instance]:
    result = await db.execute(select(Instance).order_by(Instance.name))
    return list(result.scalars().all())


@router.post("", response_model=InstanceOut, status_code=status.HTTP_201_CREATED)
async def create_instance(payload: InstanceCreate, db: AsyncSession = Depends(get_db)) -> Instance:
    existing = await db.execute(select(Instance).where(Instance.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Instance name already exists")

    data = payload.model_dump(exclude={"password", "port"})
    instance = Instance(
        **data,
        port=payload.resolved_port(),
        password=encrypt_secret(payload.password),
    )
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

        predictions_open = (
            await db.execute(
                select(func.count())
                .select_from(PredictionInsight)
                .where(
                    PredictionInsight.instance_id == instance.id,
                    PredictionInsight.acknowledged_at.is_(None),
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
        elif predictions_open:
            status_label = "warning"
        elif latest:
            util = latest.get_metric("connection_utilization_pct")
            if util is not None and float(util) >= 85:
                status_label = "warning"
            elif latest.max_connections and latest.active_connections >= latest.max_connections * 0.9:
                status_label = "warning"

        summaries.append(
            InstanceSummary(
                instance=InstanceOut.model_validate(instance),
                latest_metrics=MetricSampleOut.from_orm_sample(latest) if latest else None,
                status=status_label,
                alerts_firing=int(firing or 0),
                predictions_open=int(predictions_open or 0),
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

    updates = payload.model_dump(exclude_unset=True)
    password = updates.pop("password", None)
    for key, value in updates.items():
        setattr(instance, key, value)
    if password is not None:
        instance.password = encrypt_secret(password)
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
    collector = get_collector(payload.engine, _connection_target(payload))
    ok, message, details = await collector.test_connection()
    return ConnectionTestResult(ok=ok, message=message, details=details)


@router.post("/{instance_id}/test", response_model=ConnectionTestResult)
async def test_existing_instance(instance_id: int, db: AsyncSession = Depends(get_db)) -> ConnectionTestResult:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    collector = get_collector(DatabaseEngine(instance.engine), target)
    ok, message, details = await collector.test_connection()
    return ConnectionTestResult(ok=ok, message=message, details=details)
