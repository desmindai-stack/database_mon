from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Application, DatabaseGroup, Node
from app.schemas import DatabaseGroupCreate, DatabaseGroupOut, DatabaseGroupUpdate, GroupHealthOut, NodeOut
from app.services.cluster_health import collect_group_health

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
async def list_group_nodes(group_id: int, db: AsyncSession = Depends(get_db)) -> list[Node]:
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    result = await db.execute(select(Node).where(Node.group_id == group_id).order_by(Node.name))
    return list(result.scalars().all())


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
    return GroupHealthOut.model_validate(report)
