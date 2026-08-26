from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domain.engines import DEFAULT_DATABASES, DatabaseEngine
from app.models import Application, Customer, DatabaseGroup, Instance, Node, Server
from app.schemas import DatabaseGroupOut, WizardCreateGroupRequest
from app.services.credentials import encrypt_secret

router = APIRouter(prefix="/wizard", tags=["wizard"])

_ENGINE_SERVICE_NAME = {"postgresql": "postgresql", "sqlserver": "sqlserver", "mongodb": "mongodb"}


async def _unique_instance_name(db: AsyncSession, base: str) -> str:
    name = base
    suffix = 2
    while (await db.execute(select(Instance.id).where(Instance.name == name))).first() is not None:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


def _instance_options(node_input, engine: DatabaseEngine) -> dict | None:
    """Engine-specific connection knobs that don't have a dedicated Instance column — stored in
    Instance.options and read straight through by the collectors (ConnectionTarget.options)."""
    opts: dict = {}
    if engine == DatabaseEngine.POSTGRESQL and node_input.ssl_mode:
        opts["ssl_mode"] = node_input.ssl_mode
    if engine == DatabaseEngine.SQLSERVER and node_input.auth_type:
        opts["auth_type"] = node_input.auth_type
    if engine == DatabaseEngine.MONGODB:
        if node_input.auth_source:
            opts["authSource"] = node_input.auth_source
        if node_input.replica_set:
            opts["replica_set"] = node_input.replica_set
    return opts or None


@router.post("/database-groups", response_model=DatabaseGroupOut, status_code=status.HTTP_201_CREATED)
async def wizard_create_group(payload: WizardCreateGroupRequest, db: AsyncSession = Depends(get_db)) -> DatabaseGroup:
    """Creates a DatabaseGroup + a brand-new Server + Instance + Node for each of its nodes,
    all in one database transaction — either everything commits, or (on any error, including
    a duplicate name partway through the node list) nothing does. This is the wizard's one
    atomic "save" step; connection tests happen separately, client-side, before this is ever
    called (POST /api/instances/test, /api/servers/test-agent — see GroupDetailPage's existing
    per-node connect flow for the same pattern).

    Always creates new Server rows — it does not attach nodes to an existing server (e.g. a
    second named instance on an already-registered Windows box). That case is intentionally
    still routed through the existing per-page flows (see SORULAR.md)."""
    application = await db.get(Application, payload.application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    customer = await db.get(Customer, application.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    existing_group = await db.execute(
        select(DatabaseGroup).where(
            DatabaseGroup.application_id == payload.application_id, DatabaseGroup.name == payload.group_name
        )
    )
    if existing_group.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Group name already exists for this application")

    server_names = [n.server_name for n in payload.nodes]
    if len(server_names) != len(set(server_names)):
        raise HTTPException(status_code=400, detail="Düğüm listesinde tekrar eden sunucu adı var")

    try:
        group = DatabaseGroup(
            application_id=payload.application_id,
            name=payload.group_name,
            engine=payload.engine.value,
            topology=payload.topology.value,
            environment=payload.environment.value,
            access_name=payload.access_name,
            cluster_name=payload.cluster_name,
            vip_address=payload.vip_address,
            listener_port=payload.listener_port,
            notes=payload.notes,
        )
        db.add(group)
        await db.flush()

        cluster_options = (
            payload.cluster_options.model_dump(exclude_none=True) if payload.cluster_options else {}
        )

        for node_input in payload.nodes:
            existing_server = await db.execute(
                select(Server).where(Server.customer_id == customer.id, Server.name == node_input.server_name)
            )
            if existing_server.scalar_one_or_none():
                raise HTTPException(
                    status_code=409, detail=f"'{node_input.server_name}' adında bir sunucu bu müşteride zaten var"
                )

            server = Server(
                customer_id=customer.id,
                name=node_input.server_name,
                host=node_input.host,
                ip_address=node_input.ip_address or None,
                os=node_input.os.value,
                site=node_input.site.value,
                agent_url=node_input.agent_url or None,
                agent_token=node_input.agent_token or None,
            )
            db.add(server)
            await db.flush()

            instance_name = await _unique_instance_name(db, f"{payload.group_name}-{node_input.server_name}")
            instance = Instance(
                name=instance_name,
                engine=payload.engine.value,
                host=node_input.host,
                port=node_input.port,
                database=node_input.database or DEFAULT_DATABASES.get(payload.engine, "postgres"),
                username=node_input.db_username,
                password=encrypt_secret(node_input.db_password),
                options=_instance_options(node_input, payload.engine),
                customer_name=customer.name,
                environment=customer.type,
                application=application.name,
                # A MongoDB standalone group has no group-level cluster_name (that field only
                # gets entered for the Patroni/Always On cluster steps) — the per-node replica
                # set name is the closest equivalent, so it fills the same slot when given.
                cluster_name=payload.cluster_name or node_input.replica_set,
                role=node_input.role_hint.value if node_input.role_hint != "unknown" else None,
                services=[_ENGINE_SERVICE_NAME.get(payload.engine.value, payload.engine.value)],
                group_id=group.id,
                enabled=True,
            )
            db.add(instance)
            await db.flush()

            node = Node(
                group_id=group.id,
                server_id=server.id,
                name=node_input.server_name,
                instance_name=node_input.instance_name or None,
                port=node_input.port,
                role_hint=node_input.role_hint.value,
                options=cluster_options or None,
                instance_id=instance.id,
            )
            db.add(node)

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Sihirbaz kaydı başarısız, hiçbir şey oluşturulmadı: {exc}") from exc

    await db.refresh(group)
    return group
