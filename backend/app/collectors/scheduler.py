from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Instance
from app.services.collection import collect_instance
from app.services.dashboard_snapshot import refresh_all_group_snapshots
from app.services.settings import get_dashboard_refresh_interval

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

REFRESH_JOB_ID = "refresh_dashboard_snapshots"


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


async def start_scheduler() -> None:
    if scheduler.running:
        return
    async with SessionLocal() as session:
        refresh_interval = await get_dashboard_refresh_interval(session)

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
        seconds=refresh_interval,
        id=REFRESH_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info(
        "Collector scheduler started (collect=%ss, dashboard_refresh=%ss)",
        settings.collect_interval_seconds,
        refresh_interval,
    )


def reschedule_dashboard_refresh(seconds: int) -> None:
    """Applies a new dashboard refresh interval to the live scheduler job (no restart needed).
    A no-op if the scheduler isn't running (run_mode=api) — the persisted setting still takes
    effect the next time a worker process starts."""
    if not scheduler.running:
        return
    if scheduler.get_job(REFRESH_JOB_ID) is None:
        return
    scheduler.reschedule_job(REFRESH_JOB_ID, trigger="interval", seconds=seconds)
    logger.info("Dashboard refresh interval changed to %ss", seconds)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
