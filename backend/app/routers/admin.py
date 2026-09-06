from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import (
    AdminPasswordResetOut,
    NoiseSettingsOut,
    NoiseSettingsUpdate,
    RetentionDaysIn,
    RetentionStatusOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services.auth_deps import get_current_user
from app.services.noise_settings import get_noise_settings, set_noise_settings
from app.services.retention import get_retention_status, run_retention_cleanup, set_retention_days
from app.services.security import hash_password

# Every route here is admin-only — enforced once at inclusion time in main.py
# (dependencies=[Depends(require_admin)]), not per-endpoint.
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/retention", response_model=RetentionStatusOut)
async def get_retention(db: AsyncSession = Depends(get_db)) -> RetentionStatusOut:
    return RetentionStatusOut(**await get_retention_status(db))


@router.put("/retention", response_model=RetentionStatusOut)
async def put_retention(payload: RetentionDaysIn, db: AsyncSession = Depends(get_db)) -> RetentionStatusOut:
    try:
        await set_retention_days(db, payload.retention_days)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return RetentionStatusOut(**await get_retention_status(db))


@router.post("/retention/run", response_model=RetentionStatusOut)
async def trigger_retention_run(db: AsyncSession = Depends(get_db)) -> RetentionStatusOut:
    await run_retention_cleanup()
    return RetentionStatusOut(**await get_retention_status(db))


@router.get("/users", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[User]:
    result = await db.execute(select(User).order_by(User.username))
    return list(result.scalars().all())


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    existing = await db.execute(select(User).where(User.username == payload.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bu kullanıcı adı zaten var")
    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role.value,
        is_active=True,
        # Admin-created accounts also start with a forced password change — the admin picks
        # the initial password, but only the user themselves should end up knowing it long-term.
        must_change_password=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, payload: UserUpdate, current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> User:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kullanıcı bulunamadı")
    if user.id == current.id and (payload.is_active is False or (payload.role is not None and payload.role != "admin")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kendi hesabınızı pasifleştiremez veya yetkinizi düşüremezsiniz")
    if payload.role is not None:
        user.role = payload.role.value
    if payload.is_active is not None:
        user.is_active = payload.is_active
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int, current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    if user_id == current.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kendi hesabınızı silemezsiniz")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kullanıcı bulunamadı")
    await db.delete(user)
    await db.commit()


@router.post("/users/{user_id}/reset-password", response_model=AdminPasswordResetOut)
async def reset_password(user_id: int, db: AsyncSession = Depends(get_db)) -> AdminPasswordResetOut:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kullanıcı bulunamadı")
    temp_password = secrets.token_urlsafe(9)
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    await db.commit()
    return AdminPasswordResetOut(temporary_password=temp_password)


@router.get("/noise-settings", response_model=NoiseSettingsOut)
async def read_noise_settings(db: AsyncSession = Depends(get_db)) -> NoiseSettingsOut:
    """Rapor/DPA gürültü eşikleri (Faz 18 İŞ 2)."""
    return NoiseSettingsOut(**await get_noise_settings(db))


@router.put("/noise-settings", response_model=NoiseSettingsOut)
async def update_noise_settings(
    payload: NoiseSettingsUpdate, db: AsyncSession = Depends(get_db)
) -> NoiseSettingsOut:
    try:
        updated = await set_noise_settings(
            db,
            list_min_total_ms=payload.list_min_total_ms,
            list_min_calls=payload.list_min_calls,
            finding_min_total_ms=payload.finding_min_total_ms,
            finding_min_calls=payload.finding_min_calls,
            show_system_queries=payload.show_system_queries,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return NoiseSettingsOut(**updated)
