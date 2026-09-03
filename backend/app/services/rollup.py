"""Faz 16 İŞ 6 — günlük özet (rollup). Ham `MetricSample` satırları saklama süresinden (30 gün
varsayılan, bkz. services/retention.py) sonra silinir; uzun vadeli tahminler (disk dolma tarihi,
transaction ID wraparound, tablo büyümesi) haftalar/aylar süren bir trend ister. Bu modül her gün
bir kez (bkz. collectors/scheduler.py) DÜNÜN verisini tek satırlık özetlere indirger — bu
özetler retention temizliğinden MUAF, ham verilerden çok daha küçük hacimli.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.collectors.registry import get_collector
from app.domain.engines import DatabaseEngine
from app.models import Instance, MetricRollupDaily, MetricSample, SchemaObjectDailySample
from app.services.credentials import decrypt_secret

logger = logging.getLogger(__name__)

# Sadece tahminlerin gerçekten kullandığı metrikler rollup'lanıyor — MetricSample.metrics_json
# içindeki her anahtarı (checkpoints_*, temp_* vb.) rollup'lamak tabloyu gereksiz büyütürdü.
ROLLUP_METRIC_KEYS = ("database_size_bytes", "active_connections", "connection_utilization_pct", "transaction_id_age")

# Şema taraması (collect_table_sizes/collect_schema_health) günde bir kez çalışsa da bir
# katalog taraması — kaç nesne saklanacağını sınırlı tutuyoruz.
SCHEMA_OBJECTS_LIMIT = 20


async def _rollup_metrics_for_instance(
    session: AsyncSession, instance: Instance, day: date, window_start: datetime, window_end: datetime
) -> int:
    result = await session.execute(
        select(MetricSample).where(
            MetricSample.instance_id == instance.id,
            MetricSample.collected_at >= window_start,
            MetricSample.collected_at < window_end,
        )
    )
    samples = list(result.scalars().all())
    if not samples:
        return 0

    written = 0
    for key in ROLLUP_METRIC_KEYS:
        values = [v for v in (s.get_metric(key) for s in samples) if v is not None]
        values = [float(v) for v in values]
        if not values:
            continue
        existing = (
            await session.execute(
                select(MetricRollupDaily).where(
                    MetricRollupDaily.instance_id == instance.id,
                    MetricRollupDaily.metric_key == key,
                    MetricRollupDaily.day == day,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = MetricRollupDaily(instance_id=instance.id, metric_key=key, day=day)
            session.add(existing)
        existing.avg_value = mean(values)
        existing.min_value = min(values)
        existing.max_value = max(values)
        existing.last_value = values[-1]
        existing.sample_count = len(values)
        written += 1
    return written


async def _upsert_schema_object(
    session: AsyncSession,
    instance_id: int,
    day: date,
    object_kind: str,
    schema_name: str,
    object_name: str,
    size_bytes: float,
    extra: dict[str, Any] | None,
) -> None:
    existing = (
        await session.execute(
            select(SchemaObjectDailySample).where(
                SchemaObjectDailySample.instance_id == instance_id,
                SchemaObjectDailySample.object_kind == object_kind,
                SchemaObjectDailySample.schema_name == schema_name,
                SchemaObjectDailySample.object_name == object_name,
                SchemaObjectDailySample.day == day,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = SchemaObjectDailySample(
            instance_id=instance_id, day=day, object_kind=object_kind, schema_name=schema_name, object_name=object_name,
        )
        session.add(existing)
    existing.size_bytes = size_bytes
    existing.extra = extra


async def _rollup_schema_objects_for_instance(session: AsyncSession, instance: Instance, day: date) -> int:
    """Faz 16 İŞ 6: tablo/index boyutlarının bugünkü anlık görüntüsü — 15s'lik toplama
    döngüsünde DEĞİL, bu günlük iş içinde, tek seferlik bir katalog taramasıyla alınır (bkz.
    README "Monitoring load" — periyodik döngü dışında, günde bir kez çalışan bir tarama kabul
    edilebilir maliyette)."""
    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    collector = get_collector(DatabaseEngine(instance.engine), target)
    written = 0
    try:
        tables = await collector.collect_table_sizes(limit=SCHEMA_OBJECTS_LIMIT)
    except Exception:
        logger.warning("daily rollup: table size probe failed for instance %s", instance.name)
        tables = []
    for t in tables:
        await _upsert_schema_object(
            session, instance.id, day, "table", t["schema_name"], t["table_name"], float(t["table_bytes"]), None
        )
        written += 1

    try:
        health = await collector.collect_schema_health(limit=SCHEMA_OBJECTS_LIMIT)
    except Exception:
        logger.warning("daily rollup: schema health probe failed for instance %s", instance.name)
        health = {}
    for idx in health.get("unused_indexes", [])[:SCHEMA_OBJECTS_LIMIT]:
        await _upsert_schema_object(
            session,
            instance.id,
            day,
            "index",
            idx["schema_name"],
            f"{idx['table_name']}.{idx['index_name']}",
            float(idx["index_bytes"]),
            {"idx_scan": idx["idx_scan"]},
        )
        written += 1
    return written


async def run_daily_rollup(session: AsyncSession) -> dict[str, int]:
    """Dünün (UTC gün sınırı) verisini özetler — bugünün verisi henüz eksik olabileceğinden
    bugünü değil, tamamlanmış son günü rollup'lar."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    window_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)
    window_end = window_start + timedelta(days=1)

    instances = (await session.execute(select(Instance).where(Instance.enabled.is_(True)))).scalars().all()
    metric_rows = 0
    schema_rows = 0
    for instance in instances:
        metric_rows += await _rollup_metrics_for_instance(session, instance, yesterday, window_start, window_end)
        if instance.engine == "postgresql":
            schema_rows += await _rollup_schema_objects_for_instance(session, instance, yesterday)

    await session.commit()
    return {"metric_rows": metric_rows, "schema_rows": schema_rows}
