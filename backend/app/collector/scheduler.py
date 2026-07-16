from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.pg_collector import PostgresCollector, PostgresTarget
from app.config import settings
from app.database import SessionLocal
from app.models import Instance, MetricSample, SlowQuerySample
from app.services.alert_engine import evaluate_alerts

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_previous_state: dict[int, dict] = {}


async def collect_instance(instance: Instance, session: AsyncSession) -> None:
    collector = PostgresCollector(
        PostgresTarget(
            host=instance.host,
            port=instance.port,
            database=instance.database,
            username=instance.username,
            password=instance.password,
        )
    )

    prev = _previous_state.get(instance.id)
    if prev:
        prev["delta_time"] = settings.collect_interval_seconds

    metrics = await collector.collect_metrics(previous=prev)
    state = metrics.pop("_state", {})
    state["collected_at"] = datetime.now(UTC)
    _previous_state[instance.id] = state

    sample = MetricSample(instance_id=instance.id, **metrics)
    session.add(sample)
    await session.flush()

    slow_queries = await collector.collect_slow_queries()
    for row in slow_queries:
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

    await evaluate_alerts(session, instance.id, metrics)


async def collect_all_instances() -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Instance).where(Instance.enabled.is_(True)))
        instances = result.scalars().all()
        for instance in instances:
            try:
                await collect_instance(instance, session)
            except Exception:
                logger.exception("Failed collecting metrics for instance %s", instance.name)
        await session.commit()


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        collect_all_instances,
        "interval",
        seconds=settings.collect_interval_seconds,
        id="collect_all",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Collector scheduler started (interval=%ss)", settings.collect_interval_seconds)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
