import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Application, DatabaseGroup, Node
from app.schemas import (
    AlwaysOnHealthOut,
    DatabaseGroupCreate,
    DatabaseGroupOut,
    DatabaseGroupUpdate,
    GroupHealthOut,
    NodeOut,
    ParameterAuditOut,
)
from app.services.alert_engine import ensure_group_alert_rules, evaluate_group_alerts
from app.services.alwayson_health import collect_alwayson_health
from app.services.cluster_health import collect_group_health, group_health_metric_flags
from app.services.credentials import redact_node_options
from app.services.parameter_audit import collect_parameter_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/groups", tags=["database-groups"])


@router.get("", response_model=list[DatabaseGroupOut])
async def list_groups(
    application_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[DatabaseGroup]:
    query = select(DatabaseGroup).order_by(DatabaseGroup.name)
    if application_id is not None:
        query = query.where(DatabaseGroup.application_id == application_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("", response_model=DatabaseGroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(payload: DatabaseGroupCreate, db: AsyncSession = Depends(get_db)) -> DatabaseGroup:
    application = await db.get(Application, payload.application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    existing = await db.execute(
        select(DatabaseGroup).where(
            DatabaseGroup.application_id == payload.application_id,
            DatabaseGroup.name == payload.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Group name already exists for this application")
    group = DatabaseGroup(**payload.model_dump())
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


@router.get("/{group_id}", response_model=DatabaseGroupOut)
async def get_group(group_id: int, db: AsyncSession = Depends(get_db)) -> DatabaseGroup:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    return group


@router.patch("/{group_id}", response_model=DatabaseGroupOut)
async def update_group(
    group_id: int, payload: DatabaseGroupUpdate, db: AsyncSession = Depends(get_db)
) -> DatabaseGroup:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(group, key, value)
    await db.commit()
    await db.refresh(group)
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db)) -> None:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    await db.delete(group)
    await db.commit()


@router.get("/{group_id}/nodes", response_model=list[NodeOut])
async def list_group_nodes(group_id: int, db: AsyncSession = Depends(get_db)) -> list[NodeOut]:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    result = await db.execute(select(Node).where(Node.group_id == group_id).order_by(Node.name))
    nodes = result.scalars().all()
    outs = [NodeOut.model_validate(n) for n in nodes]
    for out in outs:
        out.options = redact_node_options(out.options)
    return outs


@router.get("/{group_id}/health", response_model=GroupHealthOut)
async def get_group_health(group_id: int, db: AsyncSession = Depends(get_db)) -> GroupHealthOut:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    nodes = list(
        (await db.execute(select(Node).where(Node.group_id == group_id).order_by(Node.name))).scalars().all()
    )
    if not nodes:
        raise HTTPException(status_code=400, detail="Group has no nodes to probe")
    try:
        report = await collect_group_health(group, nodes)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Group health failed: {exc}") from exc

    try:
        flags = group_health_metric_flags(report)
        await ensure_group_alert_rules(db, group_id)
        await evaluate_group_alerts(db, group_id, flags)
        await db.commit()
    except Exception:
        logger.exception("group alert persistence failed for group %s", group_id)
        await db.rollback()

    return GroupHealthOut.model_validate(report)


@router.get("/{group_id}/parameters", response_model=ParameterAuditOut)
async def get_group_parameters(group_id: int, db: AsyncSession = Depends(get_db)) -> ParameterAuditOut:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    nodes = list(
        (await db.execute(select(Node).where(Node.group_id == group_id).order_by(Node.name))).scalars().all()
    )
    try:
        report = await collect_parameter_audit(group, nodes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Parameter audit failed: {exc}") from exc
    return ParameterAuditOut.model_validate(report)


@router.get("/{group_id}/alwayson", response_model=AlwaysOnHealthOut)
async def get_group_alwayson(group_id: int, db: AsyncSession = Depends(get_db)) -> AlwaysOnHealthOut:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    nodes = list(
        (await db.execute(select(Node).where(Node.group_id == group_id).order_by(Node.name))).scalars().all()
    )
    try:
        report = await collect_alwayson_health(group, nodes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Always On health failed: {exc}") from exc
    return AlwaysOnHealthOut.model_validate(report)
