from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime

from app.config import settings
from app.database import SessionLocal
from app.models import Instance
from app.services.collection import collect_instance
from app.services.dashboard_snapshot import refresh_all_group_snapshots

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


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


async def refresh_dashboard_snapshots() -> None:
    async with SessionLocal() as session:
        try:
            await refresh_all_group_snapshots(session)
        except Exception:
            logger.exception("Failed refreshing dashboard group health snapshots")


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
    scheduler.add_job(
        refresh_dashboard_snapshots,
        "interval",
        seconds=settings.dashboard_refresh_interval_seconds,
        id="refresh_dashboard_snapshots",
        replace_existing=True,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info("Collector scheduler started (interval=%ss)", settings.collect_interval_seconds)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
