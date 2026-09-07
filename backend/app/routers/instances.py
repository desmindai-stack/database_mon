from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget, classify_connection_error
from app.collectors.registry import get_collector
from app.database import get_db
from app.domain.engines import DatabaseEngine
from app.domain.metrics import CANONICAL_METRICS, metrics_for_engine
from app.models import (
    AlertEvent,
    AlertRule,
    Instance,
    MetricRollupDaily,
    MetricSample,
    Node,
    PredictionInsight,
    SchemaObjectDailySample,
    SlowQuerySample,
)
from app.schemas import (
    ActivityOut,
    ClusterHealthOut,
    ClusterLogsOut,
    ConnectionTestResult,
    IgnoredPrerequisitesUpdate,
    InstanceCreate,
    InstanceDependenciesOut,
    InstanceOut,
    InstanceSummary,
    InstanceUpdate,
    LinkedNodeOut,
    MetricDefinitionOut,
    MetricSampleOut,
    PerformanceInsightOut,
    PredictionReadinessOut,
    PrerequisiteCheckOut,
    PrerequisiteReportOut,
    SchemaHealthOut,
    TuningChecklistOut,
    TuningReportOut,
)
from app.config import settings
from app.services.cluster_health import collect_cluster_health, fetch_agent_logs
from app.services.credentials import decrypt_secret, encrypt_secret
from app.services.performance_insights import analyze_metrics
from app.services.deletion import (
    clear_dependents,
    collect_dependents,
    commit_or_conflict,
    describe,
)
from app.services.prediction import compute_prediction_readiness
from app.services.prerequisites import run_prerequisite_checks

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


# Şema alanı -> tablo adı. Yalnızca GERİYE DÖNÜK uyumluluk için: arayüz bu adlandırılmış
# sayaçları okuyor. Asıl kaynak `breakdown`, o da metadata'dan türetiliyor.
_LEGACY_COUNT_FIELDS = {
    "metric_samples": "metric_samples",
    "slow_query_samples": "slow_query_samples",
    "alert_rules": "alert_rules",
    "alert_events": "alert_events",
    "predictions": "prediction_insights",
    "metric_rollups": "metric_rollup_daily",
    "schema_object_samples": "schema_object_daily_samples",
    "prediction_outcomes": "prediction_outcomes",
    "daily_state_snapshots": "daily_state_snapshots",
}


async def _collect_dependencies(db: AsyncSession, instance_id: int) -> InstanceDependenciesOut:
    """Instance'a bağlı kayıtların sayımı — silme onayında kullanıcıya gösterilir.

    Faz 23: tablo listesi artık ELLE YAZILMIYOR, `services/deletion.py` onu model
    metadata'sından türetiyor. Öncesinde liste elle tutuluyordu ve iki tablo (Faz 20'de
    eklenen `prediction_outcomes`, Faz 17'de eklenen `daily_state_snapshots`) hiç girmemişti:
    sayım "bağlı kayıt yok" diyor, silme foreign key ihlaliyle 500 veriyordu.
    """
    dependents = await collect_dependents(db, "instances", instance_id)
    breakdown = {d.table: d.count for d in dependents if d.count > 0}

    nodes = list((await db.execute(select(Node).where(Node.instance_id == instance_id))).scalars().all())
    # Düğümler ayrı listeleniyor (silinmiyor, bağlantısı kopuyor) — sayıma dahil edilmiyor.
    record_total = sum(count for table, count in breakdown.items() if table != "nodes")

    legacy = {field: breakdown.get(table, 0) for field, table in _LEGACY_COUNT_FIELDS.items()}
    return InstanceDependenciesOut(
        instance_id=instance_id,
        **legacy,
        total_records=record_total,
        breakdown=breakdown,
        linked_nodes=[LinkedNodeOut(id=n.id, name=n.name, group_id=n.group_id, port=n.port) for n in nodes],
    )


@router.get("/{instance_id}/dependencies", response_model=InstanceDependenciesOut)
async def get_instance_dependencies(
    instance_id: int, db: AsyncSession = Depends(get_db)
) -> InstanceDependenciesOut:
    """Faz 16-B İŞ 2: silmeden ÖNCE "bu instance'a bağlı ne var?" — kullanıcı neyi kaybedeceğini
    görmeden onaylamasın diye."""
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return await _collect_dependencies(db, instance_id)


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instance(
    instance_id: int,
    cascade: bool = Query(
        default=False,
        description="true ise bağlı metrik/sorgu/alarm/tahmin kayıtları ve düğüm bağlantıları da silinir",
    ),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Faz 16-B İŞ 2: silme artık gerçekten çalışıyor.

    Eskiden düz `db.delete(instance)` çağrılıyordu; instance'a işaret eden 7 tabloya (metrik,
    yavaş sorgu, alarm kuralı/olayı, tahmin, rollup, şema örneği) ve `nodes.instance_id`'ye
    foreign key kısıtı olduğu için silme veritabanı hatasıyla düşüyordu — kullanıcıya
    "kullanılmayan instance silinemiyor" olarak yansıyordu.

    Artık: bağlı kayıt yoksa doğrudan siliniyor; varsa `cascade=false` (varsayılan) ile 409
    dönüp neyin bağlı olduğunu sayılarıyla bildiriyor, `cascade=true` ile hepsi birlikte
    siliniyor. Düğümler (Node) SİLİNMEZ — sadece bağlantıları kopar (`instance_id = NULL`);
    düğüm cluster topolojisinin parçası, veritabanı kaydının değil.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    deps = await _collect_dependencies(db, instance_id)
    if (deps.total_records or deps.linked_nodes) and not cascade:
        detail = describe(await collect_dependents(db, "instances", instance_id))
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bu instance'a bağlı kayıtlar var ({detail}). "
                "Birlikte silmek için cascade=true gönderin."
            ),
        )

    if cascade:
        # Tablo listesi metadata'dan geliyor: nullable bağlar koparılıyor (düğüm cluster
        # topolojisini temsil ediyor, instance'a yeniden bağlanabilir), zorunlu olanlar
        # siliniyor. Yeni bir tablo eklendiğinde burayı güncellemek gerekmiyor.
        await clear_dependents(db, "instances", instance_id)

    await db.delete(instance)
    await commit_or_conflict(db, "instances", instance_id, "Instance")


@router.post("/{instance_id}/test-config", response_model=ConnectionTestResult)
async def test_instance_config(
    instance_id: int, payload: InstanceUpdate, db: AsyncSession = Depends(get_db)
) -> ConnectionTestResult:
    """Kaydetmeden bağlantı testi (Faz 16-B İŞ 2).

    Düzenleme formundaki "Bağlantı testi" butonu daha önce /instances/test'e gidiyordu ve şifre
    alanı boş olduğu için (form var olan şifreyi göstermez) var olan bir instance'ta hep
    başarısız oluyordu. Burada gönderilmeyen alanlar KAYITLI değerlerden tamamlanıyor: şifre boş
    bırakılırsa saklanan şifre kullanılır, doldurulursa yeni şifre denenir — hiçbir şey
    kaydedilmez.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    updates = payload.model_dump(exclude_unset=True)
    password = updates.get("password")
    engine = DatabaseEngine(updates.get("engine") or instance.engine)
    options = updates.get("options") if "options" in updates else instance.options
    target = ConnectionTarget(
        host=updates.get("host") or instance.host,
        port=int(updates.get("port") or instance.port),
        database=updates.get("database") or instance.database,
        username=updates.get("username") or instance.username,
        password=password if password else decrypt_secret(instance.password),
        options=options,
    )
    collector = get_collector(engine, target)
    ok, message, details = await collector.test_connection()
    return ConnectionTestResult(ok=ok, message=message, details=details)


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
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc
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
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc
    return SchemaHealthOut.model_validate(data)


def build_prerequisite_report(
    engine: str, checks: list, ignored_keys: set[str]
) -> PrerequisiteReportOut:
    """Ham kontrol listesinden rapor — yoksayılanlar yüzdeden düşülerek (Faz 16-B İŞ 6).

    Yoksayma kontrolü yeşile boyamaz: `status` gerçek sonucu göstermeye devam eder, sadece
    `ignored` işaretlenir ve ilerleme sayımının dışında kalır. Böylece ortamda kullanılmayacak
    bir uzantı yüzünden liste sonsuza kadar kırmızı kalmaz, ama gerçek durum da gizlenmez.
    """
    out: list[PrerequisiteCheckOut] = []
    for check in checks:
        item = PrerequisiteCheckOut(**vars(check))
        item.ignored = item.key in ignored_keys
        out.append(item)

    counted = [c for c in out if not c.ignored]
    ok_count = sum(1 for c in counted if c.status == "ok")
    return PrerequisiteReportOut(
        engine=engine,
        checked_at=datetime.now(UTC),
        checks=out,
        ok_count=ok_count,
        issue_count=len(counted) - ok_count,
        ignored_count=len(out) - len(counted),
        # Sayılacak kontrol kalmadıysa (hepsi yoksayıldıysa) %100 — kullanıcı bilinçli olarak
        # "burada denetlenecek bir şey yok" demiş oluyor.
        completion_pct=100 if not counted else round(ok_count * 100 / len(counted)),
    )


@router.get("/{instance_id}/prerequisites", response_model=PrerequisiteReportOut)
async def get_instance_prerequisites(instance_id: int, db: AsyncSession = Depends(get_db)) -> PrerequisiteReportOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.engine not in ("postgresql", "sqlserver"):
        raise HTTPException(
            status_code=400, detail="Ön koşul denetimi şu an sadece PostgreSQL ve SQL Server için mevcut"
        )

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    try:
        checks = await run_prerequisite_checks(DatabaseEngine(instance.engine), target)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc

    return build_prerequisite_report(instance.engine, checks, set(instance.ignored_prerequisites or []))


@router.put("/{instance_id}/prerequisites/ignored", response_model=list[str])
async def set_ignored_prerequisites(
    instance_id: int, payload: IgnoredPrerequisitesUpdate, db: AsyncSession = Depends(get_db)
) -> list[str]:
    """Yoksayılan ön koşul anahtarlarını instance bazında kalıcı olarak saklar (Faz 16-B İŞ 6).

    Tam liste gönderilir (idempotent): bir kontrolü yoksaymak da geri almak da aynı uçtan
    yapılır, böylece arayüzde iki ayrı çağrı ve iki ayrı hata yolu olmaz.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    # Sıralı ve tekilleştirilmiş sakla — kaydın karşılaştırılabilir olması testleri ve
    # "değişti mi?" kontrollerini basitleştiriyor.
    keys = sorted(set(payload.keys))
    instance.ignored_prerequisites = keys or None
    await db.commit()
    return keys


@router.get("/{instance_id}/prediction-readiness", response_model=list[PredictionReadinessOut])
async def get_prediction_readiness(instance_id: int, db: AsyncSession = Depends(get_db)) -> list[PredictionReadinessOut]:
    """Faz 16 İŞ 6: her tahmin türü için "kaç gün/örnek gerekli, şu an ne kadar var" —
    tahminin kendisi olmasa bile bu her zaman döner, böylece UI "neden tahmin yok" sorusunu
    her zaman cevaplayabilir."""
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    results = await compute_prediction_readiness(db, instance_id, instance.engine)
    return [PredictionReadinessOut(**vars(r)) for r in results]


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
