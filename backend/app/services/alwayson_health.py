from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.collectors.base import ConnectionTarget
from app.collectors.sqlserver_mongodb import build_odbc_connection_string
from app.models import DatabaseGroup, Node
from app.services.credentials import decrypt_node_options

logger = logging.getLogger(__name__)

_AG_STATE_SQL = """
SELECT ags.name AS ag_name, ags.primary_replica, ags.synchronization_health_desc AS ag_sync_health
FROM sys.dm_hadr_availability_group_states ags
"""

_REPLICA_STATE_SQL = """
SELECT ar.replica_server_name, ars.role_desc, ars.operational_state_desc,
       ars.connected_state_desc, ars.synchronization_health_desc AS replica_sync_health,
       ar.availability_mode_desc, ar.failover_mode_desc
FROM sys.dm_hadr_availability_replica_states ars
JOIN sys.availability_replicas ar ON ar.replica_id = ars.replica_id
"""

_DB_REPLICA_STATE_SQL = """
SELECT ar.replica_server_name, DB_NAME(dbrs.database_id) AS database_name,
       dbrs.synchronization_state_desc, dbrs.synchronization_health_desc AS db_sync_health,
       dbrs.log_send_queue_size, dbrs.redo_queue_size, dbrs.last_commit_time
FROM sys.dm_hadr_database_replica_states dbrs
JOIN sys.availability_replicas ar ON ar.replica_id = dbrs.replica_id
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _connect(node: Node):
    try:
        import aioodbc
    except ImportError as exc:
        raise RuntimeError(
            "Always On sağlık denetimi aioodbc + bir ODBC sürücüsü gerektirir "
            "(ör. 'ODBC Driver 18 for SQL Server')."
        ) from exc

    opts = decrypt_node_options(node.options) or {}
    username = opts.get("db_username")
    if not username:
        raise ValueError(
            f"Node '{node.name}' için node.options.db_username tanımlı değil "
            "(Always On denetimi bağlantı gerektirir)"
        )
    target = ConnectionTarget(
        host=node.host,
        port=node.port,
        database=opts.get("db_database", "master"),
        username=username,
        password=opts.get("db_password") or "",
        options=opts,
    )
    return await aioodbc.connect(dsn=build_odbc_connection_string(target), timeout=10, autocommit=True)


def _select_target_node(nodes: list[Node]) -> Node:
    return next((n for n in nodes if n.role_hint == "primary"), nodes[0])


def _match_node(replica_server_name: str | None, nodes: list[Node]) -> Node | None:
    name = (replica_server_name or "").strip().lower()
    if not name:
        return None
    for node in nodes:
        if node.name.strip().lower() == name or node.host.strip().lower() == name:
            return node
    return None


async def collect_alwayson_health(group: DatabaseGroup, nodes: list[Node]) -> dict[str, Any]:
    if group.engine != "sqlserver":
        raise ValueError("Always On denetimi şu anda yalnızca SQL Server grupları için destekleniyor")
    if not nodes:
        raise ValueError("Grupta düğüm yok")

    target = _select_target_node(nodes)
    conn = await _connect(target)
    try:
        async with conn.cursor() as cur:
            await cur.execute(_AG_STATE_SQL)
            ag_columns = [c[0] for c in cur.description]
            ag_rows = [dict(zip(ag_columns, row)) for row in await cur.fetchall()]

            await cur.execute(_REPLICA_STATE_SQL)
            replica_columns = [c[0] for c in cur.description]
            replica_rows = [dict(zip(replica_columns, row)) for row in await cur.fetchall()]

            await cur.execute(_DB_REPLICA_STATE_SQL)
            db_columns = [c[0] for c in cur.description]
            db_rows = [dict(zip(db_columns, row)) for row in await cur.fetchall()]
    finally:
        await conn.close()

    ag = ag_rows[0] if ag_rows else {}

    dbs_by_replica: dict[str, list[dict[str, Any]]] = {}
    for row in db_rows:
        server = row["replica_server_name"]
        dbs_by_replica.setdefault(server, []).append(
            {
                "database_name": row.get("database_name"),
                "synchronization_state": row.get("synchronization_state_desc"),
                "sync_health": row.get("db_sync_health"),
                "log_send_queue_kb": (
                    float(row["log_send_queue_size"]) if row.get("log_send_queue_size") is not None else None
                ),
                "redo_queue_kb": (
                    float(row["redo_queue_size"]) if row.get("redo_queue_size") is not None else None
                ),
                "last_commit_time": row["last_commit_time"].isoformat() if row.get("last_commit_time") else None,
            }
        )

    replicas: list[dict[str, Any]] = []
    unhealthy = False
    for row in replica_rows:
        server = row["replica_server_name"]
        matched = _match_node(server, nodes)
        availability_mode = row.get("availability_mode_desc")
        databases = dbs_by_replica.get(server, [])
        all_synchronized = bool(databases) and all(
            d["synchronization_state"] == "SYNCHRONIZED" for d in databases
        )
        failover_ready = availability_mode == "SYNCHRONOUS_COMMIT" and all_synchronized

        sync_health = row.get("replica_sync_health")
        if sync_health and sync_health != "HEALTHY":
            unhealthy = True

        replicas.append(
            {
                "node_id": matched.id if matched else None,
                "node_name": matched.name if matched else None,
                "replica_server_name": server,
                "site": matched.site if matched else None,
                "role": row.get("role_desc"),
                "operational_state": row.get("operational_state_desc"),
                "connected_state": row.get("connected_state_desc"),
                "sync_health": sync_health,
                "availability_mode": availability_mode,
                "failover_mode": row.get("failover_mode_desc"),
                "failover_ready": failover_ready,
                "databases": databases,
            }
        )

    overall = "healthy"
    ag_sync_health = ag.get("ag_sync_health")
    if unhealthy or (ag_sync_health and ag_sync_health != "HEALTHY") or not replicas:
        overall = "critical"

    return {
        "group_id": group.id,
        "group_name": group.name,
        "ag_name": ag.get("ag_name"),
        "primary_replica": ag.get("primary_replica"),
        "ag_sync_health": ag_sync_health,
        "overall": overall,
        "checked_at": _now_iso(),
        "replicas": replicas,
    }
