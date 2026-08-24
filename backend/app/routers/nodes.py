from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import DatabaseGroup, Node
from app.schemas import NodeCreate, NodeOut, NodeUpdate
from app.services.credentials import encrypt_node_options, redact_node_options

router = APIRouter(prefix="/nodes", tags=["nodes"])


def _redacted(node: Node) -> NodeOut:
    out = NodeOut.model_validate(node)
    out.options = redact_node_options(out.options)
    return out


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(payload: NodeCreate, db: AsyncSession = Depends(get_db)) -> NodeOut:
    group = await db.get(DatabaseGroup, payload.group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    data = payload.model_dump()
    data["options"] = encrypt_node_options(data.get("options"))
    node = Node(**data)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return _redacted(node)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(node_id: int, db: AsyncSession = Depends(get_db)) -> NodeOut:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return _redacted(node)


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(node_id: int, payload: NodeUpdate, db: AsyncSession = Depends(get_db)) -> NodeOut:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    updates = payload.model_dump(exclude_unset=True)
    if "options" in updates:
        updates["options"] = encrypt_node_options(updates["options"])
    for key, value in updates.items():
        setattr(node, key, value)
    await db.commit()
    await db.refresh(node)
    return _redacted(node)


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: int, db: AsyncSession = Depends(get_db)) -> None:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    await db.delete(node)
    await db.commit()
