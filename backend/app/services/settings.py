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


# --- Sağlık Raporu zamanlaması (Faz 17 İŞ 1) ---

HEALTH_REPORT_HOUR_KEY = "health_report_hour"
HEALTH_REPORT_ENABLED_KEY = "health_report_enabled"
HEALTH_REPORT_SCOPE_MODE_KEY = "health_report_scope_mode"

DEFAULT_HEALTH_REPORT_HOUR = 6
# Hangi kapsamlar için otomatik rapor üretilsin:
#   global    — tek bir "tüm sistem" raporu
#   customers — her müşteri için ayrı rapor
#   both      — ikisi birden (varsayılan; yönetici raporu müşteri bazında anlamlı)
ALLOWED_SCOPE_MODES = ["global", "customers", "both"]
DEFAULT_SCOPE_MODE = "both"


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row else None


async def _set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_health_report_schedule(session: AsyncSession) -> dict:
    hour_raw = await _get_setting(session, HEALTH_REPORT_HOUR_KEY)
    try:
        hour = int(hour_raw) if hour_raw is not None else DEFAULT_HEALTH_REPORT_HOUR
    except ValueError:
        hour = DEFAULT_HEALTH_REPORT_HOUR
    if not 0 <= hour <= 23:
        hour = DEFAULT_HEALTH_REPORT_HOUR

    enabled_raw = await _get_setting(session, HEALTH_REPORT_ENABLED_KEY)
    scope_mode = await _get_setting(session, HEALTH_REPORT_SCOPE_MODE_KEY) or DEFAULT_SCOPE_MODE
    if scope_mode not in ALLOWED_SCOPE_MODES:
        scope_mode = DEFAULT_SCOPE_MODE

    return {
        "hour": hour,
        "enabled": enabled_raw != "false",
        "scope_mode": scope_mode,
        "scope_mode_options": ALLOWED_SCOPE_MODES,
    }


async def set_health_report_schedule(
    session: AsyncSession, hour: int | None = None, enabled: bool | None = None, scope_mode: str | None = None
) -> dict:
    if hour is not None:
        if not 0 <= hour <= 23:
            raise ValueError(f"Saat 0-23 aralığında olmalı: {hour}")
        await _set_setting(session, HEALTH_REPORT_HOUR_KEY, str(hour))
    if enabled is not None:
        await _set_setting(session, HEALTH_REPORT_ENABLED_KEY, "true" if enabled else "false")
    if scope_mode is not None:
        if scope_mode not in ALLOWED_SCOPE_MODES:
            raise ValueError(f"Desteklenmeyen kapsam modu: {scope_mode}")
        await _set_setting(session, HEALTH_REPORT_SCOPE_MODE_KEY, scope_mode)
    await session.commit()
    return await get_health_report_schedule(session)
