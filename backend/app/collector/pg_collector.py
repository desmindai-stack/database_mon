from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class PostgresTarget:
    host: str
    port: int
    database: str
    username: str
    password: str


class PostgresCollector:
    def __init__(self, target: PostgresTarget) -> None:
        self.target = target

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=self.target.host,
            port=self.target.port,
            database=self.target.database,
            user=self.target.username,
            password=self.target.password,
            timeout=10,
        )

    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            conn = await self._connect()
            try:
                version = await conn.fetchval("SELECT version()")
                pg_stat = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')"
                )
                return True, "Connection successful", {
                    "version": version,
                    "pg_stat_statements": bool(pg_stat),
                }
            finally:
                await conn.close()
        except Exception as exc:
            logger.exception("Connection test failed")
            return False, str(exc), {}

    async def collect_metrics(self, previous: dict[str, float] | None = None) -> dict[str, Any]:
        conn = await self._connect()
        try:
            db_stats = await conn.fetchrow(
                """
                SELECT
                    numbackends,
                    xact_commit,
                    xact_rollback,
                    blks_hit,
                    blks_read,
                    deadlocks,
                    temp_bytes
                FROM pg_stat_database
                WHERE datname = current_database()
                """
            )
            max_conn = await conn.fetchval("SHOW max_connections")
            max_conn = int(max_conn) if max_conn else 0

            db_size = await conn.fetchval(
                "SELECT pg_database_size(current_database())"
            )

            replication_lag = await conn.fetchrow(
                """
                SELECT COALESCE(
                    pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()),
                    0
                ) AS lag_bytes
                WHERE pg_is_in_recovery()
                """
            )

            blks_hit = float(db_stats["blks_hit"] or 0)
            blks_read = float(db_stats["blks_read"] or 0)
            total_blocks = blks_hit + blks_read
            cache_hit_ratio = (blks_hit / total_blocks * 100) if total_blocks else 100.0

            xact_commit = float(db_stats["xact_commit"] or 0)
            xact_rollback = float(db_stats["xact_rollback"] or 0)
            transactions_per_sec = 0.0
            if previous and "xact_total" in previous:
                delta = (xact_commit + xact_rollback) - previous["xact_total"]
                delta_time = previous.get("delta_time", 15)
                transactions_per_sec = max(delta / delta_time, 0)

            return {
                "active_connections": int(db_stats["numbackends"] or 0),
                "max_connections": max_conn,
                "transactions_per_sec": round(transactions_per_sec, 2),
                "cache_hit_ratio": round(cache_hit_ratio, 2),
                "replication_lag_bytes": float(replication_lag["lag_bytes"]) if replication_lag else None,
                "database_size_bytes": float(db_size or 0),
                "deadlocks": int(db_stats["deadlocks"] or 0),
                "temp_bytes": float(db_stats["temp_bytes"] or 0),
                "_state": {
                    "xact_total": xact_commit + xact_rollback,
                },
            }
        finally:
            await conn.close()

    async def collect_slow_queries(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            has_ext = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')"
            )
            if not has_ext:
                return []

            rows = await conn.fetch(
                """
                SELECT
                    queryid::text,
                    LEFT(query, 2000) AS query,
                    calls,
                    total_exec_time AS total_time_ms,
                    mean_exec_time AS mean_time_ms,
                    rows
                FROM pg_stat_statements
                WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                ORDER BY mean_exec_time DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]
        finally:
            await conn.close()
