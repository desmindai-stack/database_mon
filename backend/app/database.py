import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(_async_url(settings.database_url), echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def _sqlite_add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    cols = {row[1] for row in result.fetchall()}
    if column not in cols:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


async def _sqlite_table_columns(conn, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result.fetchall()}


async def _sqlite_drop_column_if_exists(conn, table: str, column: str) -> None:
    cols = await _sqlite_table_columns(conn, table)
    if column not in cols:
        return
    try:
        await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    except Exception:
        logger.warning("could not drop column %s.%s (sqlite too old?) — leaving it in place, unused", table, column)


async def _migrate_nodes_to_server_model(conn) -> None:
    """Faz 8 (test turu) İŞ 1: Node used to BE the server (host/site/agent_* lived on it).
    Now Server is its own table and Node is instance-level, linked via server_id. For any
    existing nodes.host column (i.e. this hasn't run yet), back-fill one Server per distinct
    (customer, host) and point every node at it, then drop the columns that moved to Server."""
    cols = await _sqlite_table_columns(conn, "nodes")
    if "host" not in cols:
        return

    rows = (
        await conn.execute(
            text(
                """
                SELECT n.id, n.host, n.site, n.agent_url, n.agent_token, c.id, c.name
                FROM nodes n
                JOIN database_groups g ON g.id = n.group_id
                JOIN applications a ON a.id = g.application_id
                JOIN customers c ON c.id = a.customer_id
                WHERE n.server_id IS NULL
                """
            )
        )
    ).fetchall()

    server_cache: dict[tuple[int, str], int] = {}
    for node_id, host, site, agent_url, agent_token, customer_id, customer_name in rows:
        key = (customer_id, host)
        server_id = server_cache.get(key)
        if server_id is None:
            insert_result = await conn.execute(
                text(
                    "INSERT INTO servers (customer_id, name, host, os, site, agent_url, agent_token) "
                    "VALUES (:customer_id, :name, :host, 'linux', :site, :agent_url, :agent_token)"
                ),
                {
                    "customer_id": customer_id,
                    "name": f"{customer_name}-{host}",
                    "host": host,
                    "site": site or "primary",
                    "agent_url": agent_url,
                    "agent_token": agent_token,
                },
            )
            server_id = insert_result.lastrowid
            server_cache[key] = server_id
        await conn.execute(
            text("UPDATE nodes SET server_id = :sid WHERE id = :nid"), {"sid": server_id, "nid": node_id}
        )

    for column in ("host", "site", "agent_url", "agent_token"):
        await _sqlite_drop_column_if_exists(conn, "nodes", column)


async def migrate_schema() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    async with engine.begin() as conn:
        await _sqlite_add_column_if_missing(conn, "instances", "engine", "engine VARCHAR(32) DEFAULT 'postgresql'")
        await _sqlite_add_column_if_missing(conn, "instances", "options", "options JSON")
        await _sqlite_add_column_if_missing(conn, "instances", "customer_name", "customer_name VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "instances", "environment", "environment VARCHAR(32) DEFAULT 'public'")
        await _sqlite_add_column_if_missing(conn, "instances", "application", "application VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "instances", "cluster_name", "cluster_name VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "instances", "role", "role VARCHAR(32)")
        await _sqlite_add_column_if_missing(conn, "instances", "services", "services JSON")
        await _sqlite_add_column_if_missing(conn, "instances", "group_id", "group_id INTEGER")
        await _sqlite_add_column_if_missing(conn, "alert_rules", "group_id", "group_id INTEGER")
        await _sqlite_add_column_if_missing(conn, "alert_events", "group_id", "group_id INTEGER")
        await _sqlite_add_column_if_missing(
            conn, "database_groups", "environment", "environment VARCHAR(16) DEFAULT 'prod'"
        )
        await _sqlite_add_column_if_missing(conn, "database_groups", "access_name", "access_name VARCHAR(255)")
        await _sqlite_add_column_if_missing(conn, "nodes", "instance_id", "instance_id INTEGER")
        await _sqlite_add_column_if_missing(conn, "nodes", "server_id", "server_id INTEGER")
        await _sqlite_add_column_if_missing(conn, "nodes", "instance_name", "instance_name VARCHAR(128)")
        try:
            await _migrate_nodes_to_server_model(conn)
        except Exception:
            logger.exception("nodes -> server model backfill failed; existing nodes may be missing server_id")
        await _sqlite_add_column_if_missing(conn, "database_groups", "cluster_name", "cluster_name VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "database_groups", "vip_address", "vip_address VARCHAR(128)")
        await _sqlite_add_column_if_missing(
            conn, "alert_rules", "rule_type", "rule_type VARCHAR(16) DEFAULT 'metric'"
        )
        await _sqlite_add_column_if_missing(conn, "alert_rules", "is_default", "is_default BOOLEAN DEFAULT 0")
        await _sqlite_add_column_if_missing(
            conn, "alert_rules", "severity", "severity VARCHAR(16) DEFAULT 'warning'"
        )
        await _sqlite_add_column_if_missing(conn, "alert_rules", "engine", "engine VARCHAR(32)")
        await _sqlite_add_column_if_missing(conn, "alert_rules", "sql_query", "sql_query TEXT")
        await _sqlite_add_column_if_missing(
            conn, "alert_rules", "interval_seconds", "interval_seconds INTEGER DEFAULT 60"
        )
        await _sqlite_add_column_if_missing(conn, "alert_rules", "last_run_at", "last_run_at DATETIME")
        await _sqlite_add_column_if_missing(
            conn, "metric_samples", "metrics_json", "metrics_json JSON"
        )
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "shared_blks_hit", "shared_blks_hit INTEGER")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "shared_blks_read", "shared_blks_read INTEGER")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "local_blks_hit", "local_blks_hit INTEGER")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "local_blks_read", "local_blks_read INTEGER")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "temp_blks_read", "temp_blks_read INTEGER")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "temp_blks_written", "temp_blks_written INTEGER")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "plan_user_time", "plan_user_time FLOAT")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "plan_sys_time", "plan_sys_time FLOAT")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "exec_user_time", "exec_user_time FLOAT")
        await _sqlite_add_column_if_missing(conn, "slow_query_samples", "exec_sys_time", "exec_sys_time FLOAT")
        await _sqlite_add_column_if_missing(conn, "instances", "server_version", "server_version VARCHAR(255)")
        await _sqlite_add_column_if_missing(conn, "instances", "unsupported_metrics", "unsupported_metrics JSON")
        await _sqlite_add_column_if_missing(
            conn, "instances", "collect_interval_seconds", "collect_interval_seconds INTEGER"
        )
        await _sqlite_add_column_if_missing(conn, "database_groups", "listener_port", "listener_port INTEGER")
        await _sqlite_add_column_if_missing(conn, "servers", "ip_address", "ip_address VARCHAR(64)")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "recommendation", "recommendation TEXT")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "action", "action VARCHAR(255)")


async def init_db() -> None:
    from pathlib import Path

    from app import models  # noqa: F401

    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_schema()
