from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Instance
from app.services.collection import collect_instance, effective_collect_interval, last_collected_at
from app.services.custom_alert_rules import evaluate_custom_alert_rules
from app.services.dashboard_snapshot import refresh_all_group_snapshots
from app.services.retention import run_retention_cleanup
from app.services.settings import get_dashboard_refresh_interval

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

REFRESH_JOB_ID = "refresh_dashboard_snapshots"
RETENTION_JOB_ID = "retention_cleanup"
# Fixed tick for custom alert rules — each rule's own interval_seconds is honored inside
# evaluate_custom_alert_rules (per-rule "due" check), not by scheduling one job per rule.
CUSTOM_RULES_TICK_SECONDS = 10


async def collect_all_instances() -> None:
    """Runs on a fixed tick (settings.collect_interval_seconds — this is the minimum
    granularity, not a per-instance guarantee). Each instance is only actually collected once
    its own effective_collect_interval() has elapsed since its last run — an instance with a
    longer override (e.g. a lower-priority/less critical server) is simply skipped on the
    ticks it isn't due yet, same "per-item due-check on a shared tick" pattern
    evaluate_custom_alert_rules already uses for custom alert rules."""
    async with SessionLocal() as session:
        result = await session.execute(select(Instance).where(Instance.enabled.is_(True)))
        instances = result.scalars().all()
        now = datetime.now(UTC)
        for instance in instances:
            last_at = last_collected_at(instance.id)
            if last_at is not None:
                elapsed = (now - last_at).total_seconds()
                if elapsed < effective_collect_interval(instance):
                    continue
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


async def evaluate_custom_rules_tick() -> None:
    async with SessionLocal() as session:
        try:
            await evaluate_custom_alert_rules(session)
        except Exception:
            logger.exception("Failed evaluating custom alert rules")


async def retention_cleanup_tick() -> None:
    try:
        await run_retention_cleanup()
    except Exception:
        logger.exception("Failed running retention cleanup")


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
    scheduler.add_job(
        evaluate_custom_rules_tick,
        "interval",
        seconds=CUSTOM_RULES_TICK_SECONDS,
        id="evaluate_custom_rules",
        replace_existing=True,
    )
    scheduler.add_job(
        retention_cleanup_tick,
        "interval",
        days=1,
        id=RETENTION_JOB_ID,
        replace_existing=True,
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
