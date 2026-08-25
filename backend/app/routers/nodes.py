from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domain.engines import DEFAULT_DATABASES, DatabaseEngine
from app.models import Application, Customer, DatabaseGroup, Instance, Node
from app.schemas import NodeCreate, NodeOut, NodeUpdate
from app.services.credentials import encrypt_node_options, encrypt_secret, redact_node_options

router = APIRouter(prefix="/nodes", tags=["nodes"])

_NODE_INSTANCE_LINK_KEYS = {"instance_id", "db_username", "db_password", "db_database"}


def _redacted(node: Node) -> NodeOut:
    out = NodeOut.model_validate(node)
    out.options = redact_node_options(out.options)
    return out


async def _unique_instance_name(db: AsyncSession, base: str) -> str:
    name = base
    suffix = 2
    while (await db.execute(select(Instance.id).where(Instance.name == name))).first() is not None:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


async def _auto_create_instance(
    db: AsyncSession, group: DatabaseGroup, node_name: str, node_host: str, node_port: int, db_username: str,
    db_password: str | None, db_database: str | None,
) -> Instance:
    application = await db.get(Application, group.application_id)
    customer = await db.get(Customer, application.customer_id) if application else None
    engine = DatabaseEngine(group.engine)
    name = await _unique_instance_name(db, f"{group.name}-{node_name}")
    instance = Instance(
        name=name,
        engine=engine.value,
        host=node_host,
        port=node_port,
        database=db_database or DEFAULT_DATABASES.get(engine, "postgres"),
        username=db_username,
        password=encrypt_secret(db_password or ""),
        customer_name=customer.name if customer else None,
        environment=customer.type if customer else "public",
        application=application.name if application else None,
        cluster_name=group.name,
        role=None,
        services=["postgresql"],
        group_id=group.id,
        enabled=True,
    )
    db.add(instance)
    await db.flush()
    return instance


async def _resolve_instance_link(
    db: AsyncSession, group: DatabaseGroup, node_name: str, node_host: str, node_port: int, updates: dict
) -> int | None:
    """Pops the instance-link keys out of `updates` and returns the resolved instance_id (or
    None to leave unchanged) — either an explicit instance_id, or db_username triggers an
    auto-created Instance sharing the node's connection details."""
    instance_id = updates.pop("instance_id", None)
    db_username = updates.pop("db_username", None)
    db_password = updates.pop("db_password", None)
    db_database = updates.pop("db_database", None)

    if instance_id is not None:
        instance = await db.get(Instance, instance_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        instance.group_id = group.id
        return instance_id

    if db_username:
        instance = await _auto_create_instance(
            db, group, node_name, node_host, node_port, db_username, db_password, db_database
        )
        return instance.id

    return None


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(payload: NodeCreate, db: AsyncSession = Depends(get_db)) -> NodeOut:
    group = await db.get(DatabaseGroup, payload.group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    data = payload.model_dump()
    data["options"] = encrypt_node_options(data.get("options"))
    resolved_instance_id = await _resolve_instance_link(db, group, data["name"], data["host"], data["port"], data)
    node = Node(**data, instance_id=resolved_instance_id)
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

    if _NODE_INSTANCE_LINK_KEYS & updates.keys():
        group = await db.get(DatabaseGroup, node.group_id)
        resolved = await _resolve_instance_link(
            db, group, updates.get("name", node.name), updates.get("host", node.host),
            updates.get("port", node.port), updates,
        )
        if resolved is not None:
            node.instance_id = resolved

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
    # The linked Instance (and its metric history) is intentionally kept — it may be an
    # existing Instance the node was linked to rather than one auto-created for it.
    await db.delete(node)
    await db.commit()
