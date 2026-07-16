from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AlertEvent, AlertRule
from app.schemas import AlertEventOut, AlertRuleCreate, AlertRuleOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/rules", response_model=list[AlertRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db)) -> list[AlertRule]:
    result = await db.execute(select(AlertRule).order_by(AlertRule.created_at.desc()))
    return list(result.scalars().all())


@router.post("/rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: AlertRuleCreate, db: AsyncSession = Depends(get_db)) -> AlertRule:
    rule = AlertRule(**payload.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)) -> None:
    rule = await db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()


@router.get("/events", response_model=list[AlertEventOut])
async def list_events(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[AlertEvent]:
    query = select(AlertEvent).order_by(AlertEvent.triggered_at.desc())
    if active_only:
        query = query.where(AlertEvent.resolved_at.is_(None))
    result = await db.execute(query.limit(100))
    return list(result.scalars().all())


@router.post("/events/{event_id}/resolve", response_model=AlertEventOut)
async def resolve_event(event_id: int, db: AsyncSession = Depends(get_db)) -> AlertEvent:
    from datetime import UTC, datetime

    event = await db.get(AlertEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.resolved_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(event)
    return event
