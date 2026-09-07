from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Customer, Node, Server
from app.schemas import ConnectionTestResult, ServerAgentTestRequest, ServerCreate, ServerOut, ServerUpdate
from app.services.cluster_health import fetch_agent_snapshot
from app.services.deletion import commit_or_conflict

router = APIRouter(prefix="/servers", tags=["servers"])


async def _test_agent(agent_url: str | None, agent_token: str | None) -> ConnectionTestResult:
    if not agent_url:
        return ConnectionTestResult(ok=False, message="Bu sunucu için agent_url tanımlı değil.")
    snapshot = await fetch_agent_snapshot({"agent_url": agent_url, "agent_token": agent_token}, timeout=5.0)
    if snapshot is None:
        return ConnectionTestResult(
            ok=False, message="Agent'a ulaşılamadı — agent_url'i, token'ı ve ağ erişimini kontrol edin."
        )
    service_count = len(snapshot.get("services") or {})
    return ConnectionTestResult(
        ok=True, message=f"Agent'a ulaşıldı, {service_count} servis raporlandı.", details=snapshot
    )


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


@router.post("/test-agent", response_model=ConnectionTestResult)
async def test_agent_pre_save(payload: ServerAgentTestRequest) -> ConnectionTestResult:
    return await _test_agent(payload.agent_url, payload.agent_token)


@router.post("/{server_id}/test-agent", response_model=ConnectionTestResult)
async def test_agent_existing(server_id: int, db: AsyncSession = Depends(get_db)) -> ConnectionTestResult:
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return await _test_agent(server.agent_url, server.agent_token)


@router.get("/{server_id}/node-count", response_model=int)
async def server_node_count(server_id: int, db: AsyncSession = Depends(get_db)) -> int:
    """Lets the frontend check whether deleting a node just orphaned its Server (see
    GroupDetailPage's onDeleteNode) without needing a full node listing."""
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return (
        await db.execute(select(func.count()).select_from(Node).where(Node.server_id == server_id))
    ).scalar_one()


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
    await commit_or_conflict(db, "servers", server_id, "Sunucu")
