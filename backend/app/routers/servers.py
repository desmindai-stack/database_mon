from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Customer, Node, Server
from app.schemas import ServerCreate, ServerOut, ServerUpdate

router = APIRouter(prefix="/servers", tags=["servers"])


@router.get("", response_model=list[ServerOut])
async def list_servers(
    customer_id: int | None = Query(default=None), db: AsyncSession = Depends(get_db)
) -> list[Server]:
    query = select(Server).order_by(Server.name)
    if customer_id is not None:
        query = query.where(Server.customer_id == customer_id)
    return list((await db.execute(query)).scalars().all())


@router.post("", response_model=ServerOut, status_code=status.HTTP_201_CREATED)
async def create_server(payload: ServerCreate, db: AsyncSession = Depends(get_db)) -> Server:
    customer = await db.get(Customer, payload.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    existing = await db.execute(
        select(Server).where(Server.customer_id == payload.customer_id, Server.name == payload.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Server name already exists for this customer")
    server = Server(**payload.model_dump())
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return server


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(server_id: int, db: AsyncSession = Depends(get_db)) -> Server:
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.patch("/{server_id}", response_model=ServerOut)
async def update_server(server_id: int, payload: ServerUpdate, db: AsyncSession = Depends(get_db)) -> Server:
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(server, key, value)
    await db.commit()
    await db.refresh(server)
    return server


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: int, db: AsyncSession = Depends(get_db)) -> None:
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    in_use = (
        await db.execute(select(func.count()).select_from(Node).where(Node.server_id == server_id))
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status_code=409, detail=f"Sunucuya bağlı {in_use} düğüm var — önce onları silin veya taşıyın"
        )
    await db.delete(server)
    await db.commit()
