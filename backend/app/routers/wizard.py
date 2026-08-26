from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domain.engines import DEFAULT_DATABASES, DatabaseEngine
from app.domain.topology import GroupTopology
from app.models import Application, Customer, DatabaseGroup, Instance, Node, Server
from app.schemas import (
    DatabaseGroupOut,
    NodeOut,
    WizardAddNodesRequest,
    WizardCreateGroupRequest,
    WizardNodeInput,
)
from app.services.credentials import encrypt_secret, redact_node_options

router = APIRouter(prefix="/wizard", tags=["wizard"])

_ENGINE_SERVICE_NAME = {"postgresql": "postgresql", "sqlserver": "sqlserver", "mongodb": "mongodb"}


async def _unique_instance_name(db: AsyncSession, base: str) -> str:
    name = base
    suffix = 2
    while (await db.execute(select(Instance.id).where(Instance.name == name))).first() is not None:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


async def _unique_node_name(db: AsyncSession, group_id: int, base: str) -> str:
    """Node.name has a (group_id, name) unique constraint — a plain `server.name` collides when
    a second instance on an already-registered server (existing_server_id) is added to the same
    group it's already in."""
    name = base
    suffix = 2
    while (
        await db.execute(select(Node.id).where(Node.group_id == group_id, Node.name == name))
    ).first() is not None:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


def _instance_options(node_input: WizardNodeInput, engine: DatabaseEngine) -> dict | None:
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


async def _create_server_instance_node(
    db: AsyncSession,
    *,
    customer: Customer,
    application: Application,
    group: DatabaseGroup,
    node_input: WizardNodeInput,
    name_prefix: str,
    cluster_options: dict | None,
) -> Node:
    """Creates an Instance + Node for a single wizard node entry — the Server is either newly
    created or (when `existing_server_id` is set) reused, e.g. a second named SQL Server
    instance on a box that already hosts one. All `flush()`ed (not committed) onto the caller's
    transaction — shared by "create a new group" and "add node(s) to an existing group", both
    of which wrap this in the same commit-everything-or-roll-back-everything pattern."""
    engine = DatabaseEngine(group.engine)

    if node_input.existing_server_id is not None:
        server = await db.get(Server, node_input.existing_server_id)
        if not server or server.customer_id != customer.id:
            raise HTTPException(status_code=404, detail="Seçilen sunucu bulunamadı")
    else:
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

    instance_name = await _unique_instance_name(db, f"{name_prefix}-{node_input.server_name or server.name}")
    instance = Instance(
        name=instance_name,
        engine=engine.value,
        host=server.host,
        port=node_input.port,
        database=node_input.database or DEFAULT_DATABASES.get(engine, "postgres"),
        username=node_input.db_username,
        password=encrypt_secret(node_input.db_password),
        options=_instance_options(node_input, engine),
        customer_name=customer.name,
        environment=customer.type,
        application=application.name,
        # A MongoDB standalone group has no group-level cluster_name (that field only gets
        # entered for the Patroni/Always On cluster steps) — the per-node replica set name is
        # the closest equivalent, so it fills the same slot when given.
        cluster_name=group.cluster_name or node_input.replica_set,
        role=node_input.role_hint.value if node_input.role_hint != "unknown" else None,
        services=[_ENGINE_SERVICE_NAME.get(engine.value, engine.value)],
        group_id=group.id,
        enabled=True,
    )
    db.add(instance)
    await db.flush()

    node_base_name = node_input.server_name or (
        f"{server.name}-{node_input.instance_name}" if node_input.instance_name else server.name
    )
    node = Node(
        group_id=group.id,
        server_id=server.id,
        name=await _unique_node_name(db, group.id, node_base_name),
        instance_name=node_input.instance_name or None,
        port=node_input.port,
        role_hint=node_input.role_hint.value,
        options=cluster_options or None,
        instance_id=instance.id,
    )
    db.add(node)
    await db.flush()
    return node


async def _node_out(db: AsyncSession, node: Node) -> NodeOut:
    out = NodeOut.model_validate(node)
    out.options = redact_node_options(out.options)
    server = await db.get(Server, node.server_id) if node.server_id else None
    out.host = server.host if server else None
    out.site = server.site if server else None
    out.ip_address = server.ip_address if server else None
    return out


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

    new_server_names = [n.server_name for n in payload.nodes if n.existing_server_id is None]
    if len(new_server_names) != len(set(new_server_names)):
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
            await _create_server_instance_node(
                db,
                customer=customer,
                application=application,
                group=group,
                node_input=node_input,
                name_prefix=payload.group_name,
                cluster_options=cluster_options or None,
            )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Sihirbaz kaydı başarısız, hiçbir şey oluşturulmadı: {exc}") from exc

    await db.refresh(group)
    return group


@router.post("/groups/{group_id}/nodes", response_model=list[NodeOut], status_code=status.HTTP_201_CREATED)
async def wizard_add_nodes(
    group_id: int, payload: WizardAddNodesRequest, db: AsyncSession = Depends(get_db)
) -> list[NodeOut]:
    """Same wizard, opened in "add node(s) to an existing group" mode — engine/topology/cluster
    info all come from the group already; only new Server + Instance + Node rows are created,
    atomically (same commit-everything-or-roll-back-everything guarantee as group creation)."""
    group = await db.get(DatabaseGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Database group not found")
    if group.topology == GroupTopology.STANDALONE:
        raise HTTPException(
            status_code=400,
            detail="Standalone bir gruba düğüm eklenemez — önce 'Cluster'a dönüştür' ile cluster topolojisine geçirin",
        )
    application = await db.get(Application, group.application_id)
    customer = await db.get(Customer, application.customer_id) if application else None
    if not application or not customer:
        raise HTTPException(status_code=404, detail="Application/customer not found for this group")

    existing_count_result = await db.execute(select(Node).where(Node.group_id == group_id))
    existing_nodes = list(existing_count_result.scalars().all())
    if len(existing_nodes) + len(payload.nodes) > 8:
        raise HTTPException(status_code=400, detail="Bir grupta en fazla 8 düğüm olabilir")

    new_server_names = [n.server_name for n in payload.nodes if n.existing_server_id is None]
    if len(new_server_names) != len(set(new_server_names)):
        raise HTTPException(status_code=400, detail="Düğüm listesinde tekrar eden sunucu adı var")

    cluster_options = payload.cluster_options.model_dump(exclude_none=True) if payload.cluster_options else None
    if cluster_options is None and existing_nodes:
        # Inherit the sibling nodes' Patroni-stack settings so adding one more replica doesn't
        # require re-entering cluster-wide ports the user already configured for this group.
        cluster_options = existing_nodes[0].options

    try:
        created: list[Node] = []
        for node_input in payload.nodes:
            node = await _create_server_instance_node(
                db,
                customer=customer,
                application=application,
                group=group,
                node_input=node_input,
                name_prefix=group.name,
                cluster_options=cluster_options,
            )
            created.append(node)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Düğüm ekleme başarısız, hiçbir şey oluşturulmadı: {exc}") from exc

    # commit() expires every attribute on the just-created ORM objects — refresh before
    # reading them back (same pattern wizard_create_group uses for the group it returns).
    for node in created:
        await db.refresh(node)
    return [await _node_out(db, node) for node in created]
