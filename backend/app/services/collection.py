from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.collectors.registry import get_collector
from app.config import settings
from app.domain.engines import DatabaseEngine
from app.models import Instance, MetricSample, SlowQuerySample
from app.services.alert_engine import evaluate_alerts
from app.services.credentials import decrypt_secret
from app.services.prediction import run_predictions

logger = logging.getLogger(__name__)

_previous_state: dict[int, dict] = {}


def _target_for(instance: Instance) -> ConnectionTarget:
    return ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options or {},
    )


def _apply_metrics_to_sample(sample: MetricSample, metrics: dict) -> None:
    sample.metrics_json = {k: v for k, v in metrics.items() if not k.startswith("_")}
    sample.active_connections = int(metrics.get("active_connections") or 0)
    sample.max_connections = int(metrics.get("max_connections") or 0)
    sample.transactions_per_sec = float(metrics.get("transactions_per_sec") or metrics.get("ops_per_sec") or 0)
    sample.cache_hit_ratio = float(metrics.get("cache_hit_ratio") or 0)
    sample.replication_lag_bytes = metrics.get("replication_lag_bytes")
    sample.database_size_bytes = float(metrics.get("database_size_bytes") or 0)
    sample.deadlocks = int(metrics.get("deadlocks") or 0)
    sample.temp_bytes = float(metrics.get("temp_bytes") or 0)


async def collect_instance(instance: Instance, session: AsyncSession) -> None:
    engine = DatabaseEngine(instance.engine)
    collector = get_collector(engine, _target_for(instance))

    prev = _previous_state.get(instance.id)
    if prev:
        prev["delta_time"] = settings.collect_interval_seconds

    metrics = await collector.collect_metrics(previous=prev)
    state = metrics.pop("_state", {})
    state["collected_at"] = datetime.now(UTC)
    _previous_state[instance.id] = state

    sample = MetricSample(instance_id=instance.id)
    _apply_metrics_to_sample(sample, metrics)
    session.add(sample)
    await session.flush()

    for row in await collector.collect_slow_queries():
        session.add(
            SlowQuerySample(
                instance_id=instance.id,
                queryid=row.get("queryid"),
                query=row["query"],
                calls=int(row.get("calls") or 0),
                total_time_ms=float(row.get("total_time_ms") or 0),
                mean_time_ms=float(row.get("mean_time_ms") or 0),
                rows=int(row.get("rows") or 0),
            )
        )

    normalized = sample.metrics_json or {}
    await evaluate_alerts(session, instance.id, normalized)
    await run_predictions(
        session,
        instance.id,
        normalized,
        sample_interval_seconds=settings.collect_interval_seconds,
    )
