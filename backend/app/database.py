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


def _engine_kwargs_for(url: str) -> dict:
    kwargs: dict = {"echo": False}
    if url.startswith("postgresql+asyncpg://"):
        # Havuzdaki bağlantı sunucu tarafında kapanmış olabilir (Supabase pooler boştaki
        # bağlantıları düşürür). Ping olmadan ilk sorgu "connection was closed" ile patlıyor.
        kwargs["pool_pre_ping"] = True
        # dbace's own metadata DB (Customers/Instances/Users/...) can itself be a
        # Supabase/PgBouncer database in production — same prepared-statement incompatibility
        # as the collector connections against monitored targets (see
        # collectors/base.py::resolve_uses_pooler). SQLAlchemy's asyncpg dialect forwards
        # connect_args straight through to asyncpg.connect().
        connect_args: dict = {"statement_cache_size": 0}
        # Kaçak bir sorgu için son savunma hattı: zaman aşımı olmadan sorgu dakikalarca asılı
        # kalıyor, istemci hiçbir yanıt alamıyor ve Railway'in kenarı 502 döndürüyor — bu
        # durumda CORS başlığı da eklenmiyor, hata tarayıcıda CORS sorunu gibi görünüyor.
        # Sınır bilerek GENİŞ (varsayılan 120 sn): rapor üretimi API süreciyle aynı event
        # loop'ta çalışıyor (health_report.py `asyncio.create_task`) ve ağır toplama sorguları
        # yapıyor. Amaç yavaş sorguyu kesmek değil, ASILI KALMAYI önlemek.
        timeout_ms = max(0, settings.db_statement_timeout_seconds) * 1000
        if timeout_ms:
            connect_args["server_settings"] = {"statement_timeout": str(timeout_ms)}
        kwargs["connect_args"] = connect_args
    return kwargs


_database_url = _async_url(settings.database_url)
engine = create_async_engine(_database_url, **_engine_kwargs_for(_database_url))
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
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "lower_bound", "lower_bound FLOAT")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "upper_bound", "upper_bound FLOAT")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "seasonality", "seasonality VARCHAR(16)")
        await _sqlite_add_column_if_missing(
            conn, "instances", "ignored_prerequisites", "ignored_prerequisites JSON"
        )
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "playbook", "playbook JSON")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "advice", "advice JSON")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "method", "method VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "sample_count", "sample_count INTEGER")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "span_days", "span_days FLOAT")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "outliers_removed", "outliers_removed INTEGER")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "fit_kind", "fit_kind VARCHAR(16)")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "fit_note", "fit_note TEXT")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "eta_days_min", "eta_days_min FLOAT")
        await _sqlite_add_column_if_missing(conn, "prediction_insights", "eta_days_max", "eta_days_max FLOAT")
        # Faz 17 Ek İŞ A — bulgu durum makinesi.
        await _sqlite_add_column_if_missing(conn, "finding_acknowledgements", "finding_type", "finding_type VARCHAR(96)")
        await _sqlite_add_column_if_missing(
            conn, "finding_acknowledgements", "status", "status VARCHAR(32) DEFAULT 'ignored'"
        )
        await _sqlite_add_column_if_missing(conn, "finding_acknowledgements", "reference", "reference VARCHAR(255)")
        await _sqlite_add_column_if_missing(conn, "report_findings", "finding_type", "finding_type VARCHAR(96) DEFAULT ''")
        await _sqlite_add_column_if_missing(conn, "report_findings", "status", "status VARCHAR(32) DEFAULT 'open'")
        await _sqlite_add_column_if_missing(
            conn, "report_findings", "verification_failed", "verification_failed BOOLEAN DEFAULT 0"
        )
        await _sqlite_add_column_if_missing(conn, "report_findings", "decision_note", "decision_note TEXT")
        await _sqlite_add_column_if_missing(conn, "report_findings", "decision_reference", "decision_reference VARCHAR(255)")
        await _sqlite_add_column_if_missing(conn, "report_findings", "decision_until", "decision_until TIMESTAMP")
        await _sqlite_add_column_if_missing(conn, "report_findings", "advice", "advice JSON")
        await _sqlite_add_column_if_missing(conn, "report_findings", "link_hint", "link_hint VARCHAR(512)")
        await _sqlite_add_column_if_missing(conn, "report_findings", "note", "note VARCHAR(512)")
        await _sqlite_add_column_if_missing(conn, "report_findings", "facts", "facts JSON")
        # Eski "acknowledged" bayrağını yeni durum modeline taşı — yoksa yükseltmeden sonra
        # daha önce susturulmuş bulgular topluca kritik olarak geri döner.
        await conn.execute(
            text("UPDATE report_findings SET status = 'ignored' WHERE acknowledged = 1 AND status = 'open'")
        )
        await conn.execute(
            text("UPDATE report_findings SET status = 'resolved' WHERE change_state = 'resolved' AND status = 'open'")
        )


# Sonradan eklenen bileşik indeksler. `create_all` yalnızca eksik TABLOLARI oluşturur; var olan
# bir tabloya sonradan tanımlanmış indeksi EKLEMEZ. Bu yüzden SQLite tarafında elle yaratılıyor
# (Postgres tarafı supabase/migrations altındaki migration ile).
_COMPOSITE_INDEXES = (
    ("ix_metric_samples_instance_collected", "metric_samples", "instance_id, collected_at"),
    ("ix_slow_query_samples_instance_collected", "slow_query_samples", "instance_id, collected_at"),
)


async def _ensure_indexes(conn) -> None:
    for name, table, columns in _COMPOSITE_INDEXES:
        await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))


async def init_db() -> None:
    from pathlib import Path

    from app import models  # noqa: F401

    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await _ensure_indexes(conn)
    await migrate_schema()
