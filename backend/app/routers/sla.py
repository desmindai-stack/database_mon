"""SLA hedefleri ve takibi (Faz 28 İŞ 3b).

`created_by` istemciden alınmıyor, oturumdan yazılıyor — bir taahhüdün kim tarafından
girildiği, taahhüdün kendisi kadar önemli.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import SlaTarget, User
from app.schemas import SlaTargetCreate, SlaTargetOut, SlaTargetUpdate
from app.services.auth_deps import get_current_user
from app.services.sla import evaluate_all, evaluate_target

router = APIRouter(prefix="/sla", tags=["sla"])


def _validate(target_pct: float, period: str) -> None:
    if not (0 < target_pct <= 100):
        raise HTTPException(status_code=400, detail="Hedef yüzde 0 ile 100 arasında olmalı.")
    if period not in ("monthly", "quarterly"):
        raise HTTPException(status_code=400, detail="Dönem 'monthly' ya da 'quarterly' olmalı.")


@router.get("/targets", response_model=list[SlaTargetOut])
async def list_targets(db: AsyncSession = Depends(get_db)) -> list[SlaTarget]:
    return list((await db.execute(select(SlaTarget).order_by(SlaTarget.id))).scalars().all())


@router.get("/status", response_model=list[dict])
async def sla_status(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Tanımlı tüm hedeflerin güncel durumu (gerçekleşen, en iyi durum, kalan bütçe)."""
    return [status.to_dict() for status in await evaluate_all(db)]


@router.get("/status/{target_id}", response_model=dict)
async def sla_status_one(target_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    target = await db.get(SlaTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="SLA hedefi bulunamadı")
    return (await evaluate_target(db, target)).to_dict()


@router.post("/targets", response_model=SlaTargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: SlaTargetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SlaTarget:
    _validate(payload.target_pct, payload.period)
    existing = (
        await db.execute(
            select(SlaTarget).where(
                SlaTarget.scope_type == payload.scope_type,
                SlaTarget.scope_id.is_(None)
                if payload.scope_id is None
                else SlaTarget.scope_id == payload.scope_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # İki hedef aynı kapsama uygulanırsa hangisinin geçerli olduğu belirsiz kalır ve
        # "SLA tutuyor mu" sorusunun iki cevabı olur.
        raise HTTPException(status_code=409, detail="Bu kapsam için zaten bir SLA hedefi var.")
    target = SlaTarget(**payload.model_dump(), created_by=user.username)
    db.add(target)
    await db.commit()
    await db.refresh(target)
    return target


@router.patch("/targets/{target_id}", response_model=SlaTargetOut)
async def update_target(
    target_id: int, payload: SlaTargetUpdate, db: AsyncSession = Depends(get_db)
) -> SlaTarget:
    target = await db.get(SlaTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="SLA hedefi bulunamadı")
    data = payload.model_dump(exclude_unset=True)
    _validate(data.get("target_pct", target.target_pct), data.get("period", target.period))
    for key, value in data.items():
        setattr(target, key, value)
    target.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(target)
    return target


@router.delete("/targets/{target_id}", status_code=204)
async def delete_target(target_id: int, db: AsyncSession = Depends(get_db)) -> None:
    target = await db.get(SlaTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="SLA hedefi bulunamadı")
    await db.delete(target)
    await db.commit()
