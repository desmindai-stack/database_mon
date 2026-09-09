"""Bakım penceresi yönetimi (Faz 28 İŞ 3).

Yazma işlemleri main.py'deki `require_write_access` ile korunuyor (viewer salt-okunur).

`created_by` istemciden ALINMIYOR, oturumdan yazılıyor: "bu kesinti planlıydı" iddiasının
denetlenebilir olması bu alanın doğruluğuna bağlı ve istemcinin doldurduğu bir alan
denetlenebilir değildir.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domain.maintenance import RECURRENCE_LABELS, MaintenanceRecurrence
from app.models import MaintenanceWindow, User
from app.schemas import (
    MaintenanceWindowCreate,
    MaintenanceWindowOut,
    MaintenanceWindowUpdate,
)
from app.services.auth_deps import get_current_user
from app.services.maintenance import occurrences_in_period

router = APIRouter(prefix="/maintenance-windows", tags=["maintenance"])


def _validate(starts_at: datetime, ends_at: datetime, recurrence: str) -> None:
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="Bitiş zamanı başlangıçtan sonra olmalı.")
    if recurrence not in {str(r) for r in MaintenanceRecurrence}:
        raise HTTPException(
            status_code=400,
            detail=f"Geçersiz tekrar: {recurrence}. Seçenekler: {', '.join(RECURRENCE_LABELS)}",
        )
    # Tekrarlayan bir pencerenin süresi tekrar aralığını aşarsa pencereler üst üste biner ve
    # fiilen "hep bakımdayız" anlamına gelir. Bu, her kesintiyi planlı göstermenin en kolay
    # yolu olurdu; sessizce kabul edilmiyor.
    duration = (ends_at - starts_at).total_seconds()
    limits = {
        str(MaintenanceRecurrence.DAILY): 86400,
        str(MaintenanceRecurrence.WEEKLY): 7 * 86400,
        str(MaintenanceRecurrence.MONTHLY): 28 * 86400,
    }
    limit = limits.get(recurrence)
    if limit is not None and duration >= limit:
        raise HTTPException(
            status_code=400,
            detail=(
                "Pencere süresi tekrar aralığından kısa olmalı; aksi halde pencereler üst üste "
                "biner ve tüm kesintiler planlı görünür."
            ),
        )


@router.get("", response_model=list[MaintenanceWindowOut])
async def list_windows(
    scope_type: str | None = Query(default=None),
    scope_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[MaintenanceWindow]:
    stmt = select(MaintenanceWindow).order_by(MaintenanceWindow.starts_at.desc())
    if scope_type:
        stmt = stmt.where(MaintenanceWindow.scope_type == scope_type)
        if scope_id is not None:
            stmt = stmt.where(MaintenanceWindow.scope_id == scope_id)
    return list((await db.execute(stmt)).scalars().all())


@router.get("/upcoming", response_model=list[dict])
async def upcoming_occurrences(
    days: int = Query(default=30, ge=1, le=365), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """Önümüzdeki N gün içindeki somut bakım örnekleri.

    Tekrar KURALI saklandığı için "önümüzdeki bakımlar" ancak genişletilerek görülebiliyor;
    kullanıcının takvimde göreceği şey bu.
    """
    from datetime import timedelta

    now = datetime.now(UTC)
    windows = list(
        (
            await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.enabled.is_(True)))
        ).scalars().all()
    )
    rows: list[dict] = []
    for window in windows:
        for occurrence in occurrences_in_period(
            [window], now, now + timedelta(days=days)
        ):
            rows.append(
                {
                    "window_id": window.id,
                    "title": window.title,
                    "scope_type": window.scope_type,
                    "scope_id": window.scope_id,
                    "starts_at": occurrence.start.isoformat(),
                    "ends_at": occurrence.end.isoformat(),
                }
            )
    rows.sort(key=lambda r: r["starts_at"])
    return rows


@router.post("", response_model=MaintenanceWindowOut, status_code=status.HTTP_201_CREATED)
async def create_window(
    payload: MaintenanceWindowCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MaintenanceWindow:
    _validate(payload.starts_at, payload.ends_at, payload.recurrence)
    window = MaintenanceWindow(
        **payload.model_dump(),
        created_by=user.username,
    )
    db.add(window)
    await db.commit()
    await db.refresh(window)
    return window


@router.patch("/{window_id}", response_model=MaintenanceWindowOut)
async def update_window(
    window_id: int,
    payload: MaintenanceWindowUpdate,
    db: AsyncSession = Depends(get_db),
) -> MaintenanceWindow:
    window = await db.get(MaintenanceWindow, window_id)
    if window is None:
        raise HTTPException(status_code=404, detail="Bakım penceresi bulunamadı")
    data = payload.model_dump(exclude_unset=True)
    starts_at = data.get("starts_at", window.starts_at)
    ends_at = data.get("ends_at", window.ends_at)
    recurrence = data.get("recurrence", window.recurrence)
    _validate(starts_at, ends_at, recurrence)
    for key, value in data.items():
        setattr(window, key, value)
    window.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(window)
    return window


@router.delete("/{window_id}", status_code=204)
async def delete_window(window_id: int, db: AsyncSession = Depends(get_db)) -> None:
    window = await db.get(MaintenanceWindow, window_id)
    if window is None:
        raise HTTPException(status_code=404, detail="Bakım penceresi bulunamadı")
    await db.delete(window)
    await db.commit()
