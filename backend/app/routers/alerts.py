from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AlertEvent, AlertRule
from app.schemas import (
    AlertEventOut,
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
    ConnectionTestResult,
    CustomRuleTestRequest,
)
from app.services.custom_alert_rules import test_custom_query, validate_readonly_sql

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/rules", response_model=list[AlertRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db)) -> list[AlertRule]:
    result = await db.execute(select(AlertRule).order_by(AlertRule.created_at.desc()))
    return list(result.scalars().all())


@router.post("/rules/test-query", response_model=ConnectionTestResult)
async def test_rule_query(payload: CustomRuleTestRequest, db: AsyncSession = Depends(get_db)) -> ConnectionTestResult:
    """Runs a candidate custom-rule query against its target right now (Faz 15 İŞ 7's "sorguyu
    test et") — doesn't require the rule to be saved first."""
    if bool(payload.instance_id) == bool(payload.group_id):
        raise HTTPException(status_code=400, detail="Tam olarak bir hedef gerekli: instance_id veya group_id")
    ok, message, value = await test_custom_query(
        db, sql_query=payload.sql_query, instance_id=payload.instance_id, group_id=payload.group_id
    )
    return ConnectionTestResult(ok=ok, message=message, details={"value": value} if value is not None else {})


@router.post("/rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: AlertRuleCreate, db: AsyncSession = Depends(get_db)) -> AlertRule:
    data = payload.model_dump()

    if payload.rule_type == "custom":
        if not payload.sql_query:
            raise HTTPException(status_code=400, detail="Özel kural için sql_query zorunlu")
        if not payload.engine:
            raise HTTPException(status_code=400, detail="Özel kural için engine zorunlu")
        if bool(payload.instance_id) == bool(payload.group_id):
            raise HTTPException(
                status_code=400, detail="Özel kural tam olarak bir hedef almalı: instance_id veya group_id"
            )
        try:
            validate_readonly_sql(payload.sql_query)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data["metric"] = ""
    else:
        if not payload.metric:
            raise HTTPException(status_code=400, detail="Metrik kuralı için metric zorunlu")
        data["engine"] = None
        data["sql_query"] = None

    rule = AlertRule(**data, is_default=False)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.patch("/rules/{rule_id}", response_model=AlertRuleOut)
async def update_rule(rule_id: int, payload: AlertRuleUpdate, db: AsyncSession = Depends(get_db)) -> AlertRule:
    rule = await db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    updates = payload.model_dump(exclude_unset=True)

    if rule.is_default:
        disallowed = set(updates) - {"threshold", "enabled"}
        if disallowed:
            raise HTTPException(
                status_code=400,
                detail=f"Varsayılan kurallarda sadece eşik değeri ve aç/kapa düzenlenebilir (izin verilmeyen: {', '.join(sorted(disallowed))})",
            )
    elif rule.rule_type == "custom" and "sql_query" in updates and updates["sql_query"]:
        try:
            validate_readonly_sql(updates["sql_query"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    for key, value in updates.items():
        setattr(rule, key, value)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)) -> None:
    rule = await db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.is_default:
        raise HTTPException(status_code=400, detail="Varsayılan kurallar silinemez")
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
