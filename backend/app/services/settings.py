from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSetting

DASHBOARD_REFRESH_INTERVAL_KEY = "dashboard_refresh_interval_seconds"

# Sağlık durumu yenileme aralığı seçenekleri (saniye): 10sn, 30sn, 1dk, 5dk, 15dk, 1saat.
ALLOWED_REFRESH_INTERVALS: list[int] = [10, 30, 60, 300, 900, 3600]


async def get_dashboard_refresh_interval(session: AsyncSession) -> int:
    row = await session.get(AppSetting, DASHBOARD_REFRESH_INTERVAL_KEY)
    if row is not None:
        try:
            value = int(row.value)
            if value in ALLOWED_REFRESH_INTERVALS:
                return value
        except ValueError:
            pass
    return settings.dashboard_refresh_interval_seconds


async def set_dashboard_refresh_interval(session: AsyncSession, seconds: int) -> None:
    if seconds not in ALLOWED_REFRESH_INTERVALS:
        raise ValueError(f"Desteklenmeyen aralık: {seconds} (izin verilenler: {ALLOWED_REFRESH_INTERVALS})")
    row = await session.get(AppSetting, DASHBOARD_REFRESH_INTERVAL_KEY)
    if row is None:
        row = AppSetting(key=DASHBOARD_REFRESH_INTERVAL_KEY, value=str(seconds))
        session.add(row)
    else:
        row.value = str(seconds)
    await session.commit()
