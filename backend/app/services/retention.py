from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import (
    ActiveSessionMinute,
    CapturedPlan,
    AlertEvent,
    AppSetting,
    MetricSample,
    PredictionInsight,
    SlowQuerySample,
    WaitQuerySignature,
    WaitSampleMinute,
)

logger = logging.getLogger(__name__)

RETENTION_DAYS_KEY = "metrics_retention_days"
RETENTION_LAST_RUN_KEY = "retention_last_run_at"
RETENTION_LAST_DELETED_KEY = "retention_last_deleted_count"

# Saklama süresi seçenekleri (gün): 1 hafta, 2 hafta, 1 ay (varsayılan), 2 ay, 3 ay.
ALLOWED_RETENTION_DAYS: list[int] = [7, 14, 30, 60, 90]
DEFAULT_RETENTION_DAYS = 30


async def get_retention_days(session: AsyncSession) -> int:
    row = await session.get(AppSetting, RETENTION_DAYS_KEY)
    if row is not None:
        try:
            value = int(row.value)
            if value in ALLOWED_RETENTION_DAYS:
                return value
        except ValueError:
            pass
    return DEFAULT_RETENTION_DAYS


async def set_retention_days(session: AsyncSession, days: int) -> None:
    if days not in ALLOWED_RETENTION_DAYS:
        raise ValueError(f"Desteklenmeyen saklama süresi: {days} (izin verilenler: {ALLOWED_RETENTION_DAYS})")
    row = await session.get(AppSetting, RETENTION_DAYS_KEY)
    if row is None:
        session.add(AppSetting(key=RETENTION_DAYS_KEY, value=str(days)))
    else:
        row.value = str(days)
    await session.commit()


async def get_retention_status(session: AsyncSession) -> dict:
    days = await get_retention_days(session)
    last_run_row = await session.get(AppSetting, RETENTION_LAST_RUN_KEY)
    last_deleted_row = await session.get(AppSetting, RETENTION_LAST_DELETED_KEY)
    return {
        "retention_days": days,
        "options": ALLOWED_RETENTION_DAYS,
        "last_run_at": last_run_row.value if last_run_row else None,
        "last_deleted_count": int(last_deleted_row.value) if last_deleted_row else None,
    }


async def run_retention_cleanup() -> int:
    """Saklama penceresinden eski satırları siler (Faz 15 İŞ 2; Faz 25'te bekleme örnekleri
    eklendi). Runs daily via the collector scheduler and can
    also be triggered on demand from the admin screen. Records its own last-run time/count in
    AppSetting so the admin screen can show them without a dedicated audit table."""
    async with SessionLocal() as session:
        days = await get_retention_days(session)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        total_deleted = 0
        for model, ts_column in (
            (MetricSample, MetricSample.collected_at),
            (SlowQuerySample, SlowQuerySample.collected_at),
            (AlertEvent, AlertEvent.triggered_at),
            (PredictionInsight, PredictionInsight.created_at),
            # Faz 25 İŞ 1: bekleme örnekleri. Dakikalık toplandığı için ham örneklerden çok
            # daha küçük, ama SINIRSIZ değil — instance başına dakikada onlarca satır, ayda
            # yüz binler. Saklama politikasının dışında bırakmak, `slow_query_samples`'ta
            # yaşanan birikmenin (tek instance için 337 bin satır) tekrarı olurdu.
            (WaitSampleMinute, WaitSampleMinute.minute),
            (ActiveSessionMinute, ActiveSessionMinute.minute),
            # Sözlük satırı, o sorgu artık hiç görülmüyorsa anlamsız kalıyor — `last_seen_at`
            # üzerinden aynı pencereye tabi.
            (WaitQuerySignature, WaitQuerySignature.last_seen_at),
            # Faz 26 İŞ 1: yakalanan planlar. Plan JSON'u satır başına kilobaytlar tutuyor —
            # saklama politikasının dışında bırakmak, tabloyu en hızlı büyüyen tablo yapardı.
            (CapturedPlan, CapturedPlan.captured_at),
        ):
            result = await session.execute(delete(model).where(ts_column < cutoff))
            total_deleted += result.rowcount or 0

        now_iso = datetime.now(UTC).isoformat()
        for key, value in ((RETENTION_LAST_RUN_KEY, now_iso), (RETENTION_LAST_DELETED_KEY, str(total_deleted))):
            row = await session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        await session.commit()
        logger.info("Retention cleanup: deleted %d rows older than %d days", total_deleted, days)
        return total_deleted
