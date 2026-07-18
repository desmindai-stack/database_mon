from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PredictionInsight
from app.schemas import PredictionOut

router = APIRouter(prefix="/predictions", tags=["predictions"])


@router.get("", response_model=list[PredictionOut])
async def list_predictions(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[PredictionInsight]:
    query = select(PredictionInsight).order_by(PredictionInsight.created_at.desc())
    if active_only:
        query = query.where(PredictionInsight.acknowledged_at.is_(None))
    result = await db.execute(query.limit(100))
    return list(result.scalars().all())


@router.post("/{prediction_id}/ack", response_model=PredictionOut)
async def acknowledge_prediction(
    prediction_id: int, db: AsyncSession = Depends(get_db)
) -> PredictionInsight:
    from datetime import UTC, datetime

    row = await db.get(PredictionInsight, prediction_id)
    if not row:
        raise HTTPException(status_code=404, detail="Prediction not found")
    row.acknowledged_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row
