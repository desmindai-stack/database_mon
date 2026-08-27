from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.collectors.registry import get_collector
from app.config import settings
from app.domain.engines import DatabaseEngine
from app.models import Instance, MetricSample, SlowQuerySample
from app.services.alert_engine import ensure_cluster_alert_rules, evaluate_alerts
from app.services.cluster_health import (
    cluster_health_metric_flags,
    collect_cluster_health,
    compact_cluster_snapshot,
)
from app.services.credentials import decrypt_secret
from app.services.prediction import run_predictions

logger = logging.getLogger(__name__)

_previous_state: dict[int, dict] = {}


def effective_collect_interval(instance: Instance) -> int:
    """The per-instance override (Instance.collect_interval_seconds) if set, else the
    app-wide settings.collect_interval_seconds default."""
    return instance.collect_interval_seconds or settings.collect_interval_seconds


def last_collected_at(instance_id: int) -> datetime | None:
    """Used by collect_all_instances() to decide whether an instance is due yet under its own
    (possibly overridden) interval — the scheduler tick itself stays fixed/fast (see
    collectors/scheduler.py), individual instances are skipped on ticks they're not due on,
    mirroring the existing per-rule due-check pattern in evaluate_custom_alert_rules."""
    return _previous_state.get(instance_id, {}).get("collected_at")


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
    interval = effective_collect_interval(instance)

    now = datetime.now(UTC)
    prev = _previous_state.get(instance.id)
    if prev:
        # Real elapsed wall-clock time, not the nominal configured interval — a scheduler
        # tick that ran late (busy server, backlog) would otherwise skew every *_per_sec
        # metric this cycle. Falls back to the configured interval only if collected_at is
        # somehow missing from a previous cycle's state.
        prev_collected_at = prev.get("collected_at")
        prev["delta_time"] = (now - prev_collected_at).total_seconds() if prev_collected_at else interval

    # One connection shared across collect_metrics()/collect_slow_queries() instead of each
    # opening its own — halves the connections-per-cycle for engines that support it
    # (PostgreSQL, SQL Server; returns None for engines that don't, e.g. MongoDB, which then
    # fall back to managing their own connection per call exactly as before).
    conn = await collector.open_connection()
    try:
        metrics = await collector.collect_metrics(previous=prev, conn=conn)
        slow_query_rows = await collector.collect_slow_queries(conn=conn)
    finally:
        await collector.close_connection(conn)

    state = metrics.pop("_state", {})
    state["collected_at"] = now
    _previous_state[instance.id] = state

    # Collector-derived version/capability info (see collectors/postgresql.py,
    # collectors/sqlserver_mongodb.py) — persisted on Instance itself (not per-sample) since
    # it's a property of the server, refreshed on every successful collection.
    server_version = metrics.pop("_server_version", None)
    if server_version:
        instance.server_version = server_version
    instance.unsupported_metrics = metrics.pop("_unsupported_metrics", None) or None
    metrics.pop("_server_version_num", None)

    sample = MetricSample(instance_id=instance.id)
    _apply_metrics_to_sample(sample, metrics)
    session.add(sample)
    await session.flush()

    for row in slow_query_rows:
        session.add(
            SlowQuerySample(
                instance_id=instance.id,
                queryid=row.get("queryid"),
                query=row["query"],
                calls=int(row.get("calls") or 0),
                total_time_ms=float(row.get("total_time_ms") or 0),
                mean_time_ms=float(row.get("mean_time_ms") or 0),
                rows=int(row.get("rows") or 0),
                shared_blks_hit=int(row["shared_blks_hit"]) if row.get("shared_blks_hit") is not None else None,
                shared_blks_read=int(row["shared_blks_read"]) if row.get("shared_blks_read") is not None else None,
                local_blks_hit=int(row["local_blks_hit"]) if row.get("local_blks_hit") is not None else None,
                local_blks_read=int(row["local_blks_read"]) if row.get("local_blks_read") is not None else None,
                temp_blks_read=int(row["temp_blks_read"]) if row.get("temp_blks_read") is not None else None,
                temp_blks_written=int(row["temp_blks_written"]) if row.get("temp_blks_written") is not None else None,
                plan_user_time=float(row["plan_user_time"]) if row.get("plan_user_time") is not None else None,
                plan_sys_time=float(row["plan_sys_time"]) if row.get("plan_sys_time") is not None else None,
                exec_user_time=float(row["exec_user_time"]) if row.get("exec_user_time") is not None else None,
                exec_sys_time=float(row["exec_sys_time"]) if row.get("exec_sys_time") is not None else None,
            )
        )

    # Cluster/Patroni stack health snapshot (probe + optional host agent)
    try:
        if instance.cluster_name or (instance.services and len(instance.services) > 0):
            report = await collect_cluster_health(instance)
            flags = cluster_health_metric_flags(report)
            metrics_json = dict(sample.metrics_json or {})
            metrics_json["cluster_services"] = compact_cluster_snapshot(report)
            metrics_json.update(flags)
            sample.metrics_json = metrics_json
            await ensure_cluster_alert_rules(session, instance.id)
    except Exception as exc:
        logger.warning("cluster health collect failed for instance %s: %s", instance.id, exc)

    normalized = sample.metrics_json or {}
    await evaluate_alerts(session, instance.id, normalized)
    await run_predictions(
        session,
        instance.id,
        normalized,
        engine=instance.engine,
        sample_interval_seconds=interval,
    )
