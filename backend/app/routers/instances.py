from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.collectors.registry import get_collector
from app.database import get_db
from app.domain.engines import DatabaseEngine
from app.domain.metrics import CANONICAL_METRICS, metrics_for_engine
from app.models import AlertEvent, Instance, MetricSample, PredictionInsight, SlowQuerySample
from app.schemas import (
    ActivityOut,
    ClusterHealthOut,
    ClusterLogsOut,
    ConnectionTestResult,
    InstanceCreate,
    InstanceOut,
    InstanceSummary,
    InstanceUpdate,
    MetricDefinitionOut,
    MetricSampleOut,
    PerformanceInsightOut,
    SchemaHealthOut,
    TuningChecklistOut,
    TuningReportOut,
)
from app.config import settings
from app.services.cluster_health import collect_cluster_health, fetch_agent_logs
from app.services.credentials import decrypt_secret, encrypt_secret
from app.services.performance_insights import analyze_metrics

router = APIRouter(prefix="/instances", tags=["instances"])


def _apply_private_tenant(data: dict) -> dict:
    if settings.deployment_mode == "private" and settings.default_customer_name:
        data["customer_name"] = settings.default_customer_name
    return data


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
    data = _apply_private_tenant(data)
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
    updates = _apply_private_tenant(updates)
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


@router.get("/{instance_id}/activity", response_model=ActivityOut)
async def get_instance_activity(instance_id: int, db: AsyncSession = Depends(get_db)) -> ActivityOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="Activity view is currently PostgreSQL-only")

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    collector = get_collector(DatabaseEngine(instance.engine), target)
    try:
        data = await collector.collect_activity(limit=100)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Activity collection failed: {exc}") from exc
    return ActivityOut.model_validate(data)


@router.get("/{instance_id}/cluster-health", response_model=ClusterHealthOut)
async def get_cluster_health(instance_id: int, db: AsyncSession = Depends(get_db)) -> ClusterHealthOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    try:
        report = await collect_cluster_health(instance)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cluster health failed: {exc}") from exc
    return ClusterHealthOut.model_validate(report)


@router.get("/{instance_id}/cluster-logs", response_model=ClusterLogsOut)
async def get_cluster_logs(
    instance_id: int,
    service: str = Query(default="patroni"),
    lines: int = Query(default=100, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
) -> ClusterLogsOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    opts = instance.options or {}
    try:
        payload = await fetch_agent_logs(opts, service=service, lines=lines)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cluster logs failed: {exc}") from exc
    return ClusterLogsOut.model_validate(
        {
            "service": service,
            "unit": payload.get("unit"),
            "lines": payload.get("lines") or [],
            "error": payload.get("error"),
        }
    )


@router.get("/{instance_id}/schema-health", response_model=SchemaHealthOut)
async def get_schema_health(instance_id: int, db: AsyncSession = Depends(get_db)) -> SchemaHealthOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="Schema health is currently PostgreSQL-only")

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    collector = get_collector(DatabaseEngine(instance.engine), target)
    try:
        data = await collector.collect_schema_health(limit=50)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Schema health collection failed: {exc}") from exc
    return SchemaHealthOut.model_validate(data)


@router.get("/{instance_id}/insights", response_model=TuningReportOut)
async def get_instance_insights(instance_id: int, db: AsyncSession = Depends(get_db)) -> TuningReportOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    latest = (
        await db.execute(
            select(MetricSample)
            .where(MetricSample.instance_id == instance_id)
            .order_by(MetricSample.collected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    metrics: dict = dict(latest.metrics_json or {}) if latest else {}
    if latest:
        # Ensure column-level fields are available even if JSON is sparse.
        metrics.setdefault("active_connections", latest.active_connections)
        metrics.setdefault("max_connections", latest.max_connections)
        metrics.setdefault("transactions_per_sec", latest.transactions_per_sec)
        metrics.setdefault("cache_hit_ratio", latest.cache_hit_ratio)
        metrics.setdefault("replication_lag_bytes", latest.replication_lag_bytes)
        metrics.setdefault("database_size_bytes", latest.database_size_bytes)
        metrics.setdefault("deadlocks", latest.deadlocks)
        metrics.setdefault("temp_bytes", latest.temp_bytes)
        if latest.max_connections:
            metrics.setdefault(
                "connection_utilization_pct",
                (latest.active_connections / latest.max_connections) * 100,
            )

    latest_q_at = (
        await db.execute(
            select(SlowQuerySample.collected_at)
            .where(SlowQuerySample.instance_id == instance_id)
            .order_by(SlowQuerySample.collected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    slow_rows: list[dict] = []
    if latest_q_at is not None:
        q_result = await db.execute(
            select(SlowQuerySample)
            .where(
                SlowQuerySample.instance_id == instance_id,
                SlowQuerySample.collected_at == latest_q_at,
            )
            .order_by(SlowQuerySample.total_time_ms.desc())
            .limit(20)
        )
        slow_rows = [
            {
                "query": r.query,
                "calls": r.calls,
                "total_time_ms": r.total_time_ms,
                "mean_time_ms": r.mean_time_ms,
            }
            for r in q_result.scalars().all()
        ]

    report = analyze_metrics(
        metrics,
        slow_queries=slow_rows,
        collected_at=latest.collected_at if latest else None,
    )
    return TuningReportOut(
        health_score=report.health_score,
        grade=report.grade,
        status=report.status,
        collected_at=report.collected_at,
        summary=report.summary,
        insights=[
            PerformanceInsightOut(
                severity=i.severity,
                category=i.category,
                title=i.title,
                description=i.description,
                recommendation=i.recommendation,
                metric_value=i.metric_value,
                metric_unit=i.metric_unit,
                action=i.action,
            )
            for i in report.insights
        ],
        checklist=[
            TuningChecklistOut(key=c.key, label=c.label, status=c.status, detail=c.detail)
            for c in report.checklist
        ],
    )
