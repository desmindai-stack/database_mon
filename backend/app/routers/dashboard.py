from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.scheduler import reschedule_dashboard_refresh
from app.database import get_db
from app.schemas import DashboardSummaryOut, RefreshIntervalIn, RefreshIntervalOut
from app.services.dashboard import collect_dashboard_summary
from app.services.dashboard_snapshot import refresh_all_group_snapshots
from app.services.settings import (
    ALLOWED_REFRESH_INTERVALS,
    get_dashboard_refresh_interval,
    set_dashboard_refresh_interval,
)

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


@router.get("/refresh-interval", response_model=RefreshIntervalOut)
async def get_refresh_interval(db: AsyncSession = Depends(get_db)) -> RefreshIntervalOut:
    seconds = await get_dashboard_refresh_interval(db)
    return RefreshIntervalOut(seconds=seconds, options=ALLOWED_REFRESH_INTERVALS)


@router.put("/refresh-interval", response_model=RefreshIntervalOut)
async def put_refresh_interval(
    payload: RefreshIntervalIn, db: AsyncSession = Depends(get_db)
) -> RefreshIntervalOut:
    try:
        await set_dashboard_refresh_interval(db, payload.seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reschedule_dashboard_refresh(payload.seconds)
    return RefreshIntervalOut(seconds=payload.seconds, options=ALLOWED_REFRESH_INTERVALS)
