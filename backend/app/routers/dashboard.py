from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import DashboardSummaryOut
from app.services.dashboard import collect_dashboard_summary
from app.services.dashboard_snapshot import refresh_all_group_snapshots

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummaryOut)
async def get_dashboard_summary(db: AsyncSession = Depends(get_db)) -> DashboardSummaryOut:
    report = await collect_dashboard_summary(db)
    return DashboardSummaryOut.model_validate(report)


@router.post("/refresh", response_model=DashboardSummaryOut)
async def refresh_dashboard(db: AsyncSession = Depends(get_db)) -> DashboardSummaryOut:
    """Manual refresh: live-probes every group now, then returns the freshly-cached summary."""
    await refresh_all_group_snapshots(db)
    report = await collect_dashboard_summary(db)
    return DashboardSummaryOut.model_validate(report)
