from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import DashboardSummaryOut
from app.services.dashboard import collect_dashboard_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummaryOut)
async def get_dashboard_summary(db: AsyncSession = Depends(get_db)) -> DashboardSummaryOut:
    report = await collect_dashboard_summary(db)
    return DashboardSummaryOut.model_validate(report)
