from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.collectors.base import BaseCollector, ConnectionTarget

logger = logging.getLogger(__name__)


class PostgreSQLCollector(BaseCollector):
    def __init__(self, target: ConnectionTarget) -> None:
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
                    "engine": "postgresql",
                    "version": version,
                    "pg_stat_statements": bool(pg_stat),
                }
            finally:
                await conn.close()
        except Exception as exc:
            logger.exception("PostgreSQL connection test failed")
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
                    temp_bytes,
                    temp_files,
                    tup_returned,
                    tup_fetched,
                    tup_inserted,
                    tup_updated,
                    tup_deleted,
                    conflicts
                FROM pg_stat_database
                WHERE datname = current_database()
                """
            )
            max_conn = await conn.fetchval("SHOW max_connections")
            max_conn = int(max_conn) if max_conn else 0
            active = int(db_stats["numbackends"] or 0)

            db_size = await conn.fetchval("SELECT pg_database_size(current_database())")

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
            blks_read_per_sec = 0.0
            blks_hit_per_sec = 0.0
            tup_returned_per_sec = 0.0
            tup_fetched_per_sec = 0.0
            temp_bytes_per_sec = 0.0
            temp_files_per_sec = 0.0

            delta_time = previous.get("delta_time", 15) if previous else 15
            if previous:
                transactions_per_sec = max((xact_commit + xact_rollback - previous.get("xact_total", 0)) / delta_time, 0)
                blks_read_per_sec = max((blks_read - previous.get("blks_read", 0)) / delta_time, 0)
                blks_hit_per_sec = max((blks_hit - previous.get("blks_hit", 0)) / delta_time, 0)
                tup_returned_per_sec = max((float(db_stats["tup_returned"] or 0) - previous.get("tup_returned", 0)) / delta_time, 0)
                tup_fetched_per_sec = max((float(db_stats["tup_fetched"] or 0) - previous.get("tup_fetched", 0)) / delta_time, 0)
                temp_bytes_per_sec = max((float(db_stats["temp_bytes"] or 0) - previous.get("temp_bytes", 0)) / delta_time, 0)
                temp_files_per_sec = max((float(db_stats["temp_files"] or 0) - previous.get("temp_files", 0)) / delta_time, 0)

            util = (active / max_conn * 100) if max_conn else 0.0

            metrics = {
                "active_connections": active,
                "max_connections": max_conn,
                "connection_utilization_pct": round(util, 2),
                "transactions_per_sec": round(transactions_per_sec, 2),
                "cache_hit_ratio": round(cache_hit_ratio, 2),
                "replication_lag_bytes": float(replication_lag["lag_bytes"]) if replication_lag else None,
                "database_size_bytes": float(db_size or 0),
                "deadlocks": int(db_stats["deadlocks"] or 0),
                "temp_bytes": float(db_stats["temp_bytes"] or 0),
                "temp_files": int(db_stats["temp_files"] or 0),
                "tup_returned_per_sec": round(tup_returned_per_sec, 2),
                "tup_fetched_per_sec": round(tup_fetched_per_sec, 2),
                "tup_inserted_per_sec": round(
                    max((float(db_stats["tup_inserted"] or 0) - (previous.get("tup_inserted", 0) if previous else 0)) / delta_time, 0), 2
                ) if previous else 0.0,
                "tup_updated_per_sec": round(
                    max((float(db_stats["tup_updated"] or 0) - (previous.get("tup_updated", 0) if previous else 0)) / delta_time, 0), 2
                ) if previous else 0.0,
                "tup_deleted_per_sec": round(
                    max((float(db_stats["tup_deleted"] or 0) - (previous.get("tup_deleted", 0) if previous else 0)) / delta_time, 0), 2
                ) if previous else 0.0,
                "blks_read_per_sec": round(blks_read_per_sec, 2),
                "blks_hit_per_sec": round(blks_hit_per_sec, 2),
                "temp_bytes_per_sec": round(temp_bytes_per_sec, 2),
                "temp_files_per_sec": round(temp_files_per_sec, 2),
                "conflicts": int(db_stats["conflicts"] or 0),
            }

            # Background writer / checkpoint stats
            try:
                bgwriter = await conn.fetchrow(
                    """
                    SELECT
                        checkpoints_timed,
                        checkpoints_req,
                        checkpoint_write_time,
                        checkpoint_sync_time,
                        buffers_checkpoint,
                        buffers_clean,
                        buffers_backend,
                        buffers_backend_fsync,
                        buffers_alloc
                    FROM pg_stat_bgwriter
                    """
                )
                if bgwriter:
                    metrics.update({
                        "checkpoints_timed": int(bgwriter["checkpoints_timed"] or 0),
                        "checkpoints_req": int(bgwriter["checkpoints_req"] or 0),
                        "checkpoint_write_time_ms": float(bgwriter["checkpoint_write_time"] or 0),
                        "checkpoint_sync_time_ms": float(bgwriter["checkpoint_sync_time"] or 0),
                        "buffers_checkpoint_per_sec": round(
                            max((int(bgwriter["buffers_checkpoint"] or 0) - (previous.get("buffers_checkpoint", 0) if previous else 0)) / delta_time, 0), 2
                        ) if previous else 0.0,
                        "buffers_clean_per_sec": round(
                            max((int(bgwriter["buffers_clean"] or 0) - (previous.get("buffers_clean", 0) if previous else 0)) / delta_time, 0), 2
                        ) if previous else 0.0,
                        "buffers_backend_per_sec": round(
                            max((int(bgwriter["buffers_backend"] or 0) - (previous.get("buffers_backend", 0) if previous else 0)) / delta_time, 0), 2
                        ) if previous else 0.0,
                        "buffers_alloc_per_sec": round(
                            max((int(bgwriter["buffers_alloc"] or 0) - (previous.get("buffers_alloc", 0) if previous else 0)) / delta_time, 0), 2
                        ) if previous else 0.0,
                    })
            except Exception as exc:
                logger.debug("bgwriter collection failed: %s", exc)

            # pg_stat_io (PostgreSQL 16+)
            try:
                io_stats = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(SUM(reads), 0) AS reads,
                        COALESCE(SUM(writes), 0) AS writes,
                        COALESCE(SUM(extends), 0) AS extends,
                        COALESCE(MAX(op_bytes), 0) AS op_bytes
                    FROM pg_stat_io
                    WHERE context = 'normal' AND object = 'relation'
                    """
                )
                if io_stats:
                    reads = int(io_stats["reads"] or 0)
                    writes = int(io_stats["writes"] or 0)
                    extends = int(io_stats["extends"] or 0)
                    metrics.update({
                        "io_reads_per_sec": round(max((reads - (previous.get("io_reads", 0) if previous else 0)) / delta_time, 0), 2) if previous else 0.0,
                        "io_writes_per_sec": round(max((writes - (previous.get("io_writes", 0) if previous else 0)) / delta_time, 0), 2) if previous else 0.0,
                        "io_extends_per_sec": round(max((extends - (previous.get("io_extends", 0) if previous else 0)) / delta_time, 0), 2) if previous else 0.0,
                        "io_op_bytes": int(io_stats["op_bytes"] or 0),
                    })
                    metrics["_io_reads"] = reads
                    metrics["_io_writes"] = writes
                    metrics["_io_extends"] = extends
            except Exception as exc:
                logger.debug("pg_stat_io collection failed: %s", exc)

            state = {
                "xact_total": xact_commit + xact_rollback,
                "blks_read": blks_read,
                "blks_hit": blks_hit,
                "tup_returned": float(db_stats["tup_returned"] or 0),
                "tup_fetched": float(db_stats["tup_fetched"] or 0),
                "tup_inserted": float(db_stats["tup_inserted"] or 0),
                "tup_updated": float(db_stats["tup_updated"] or 0),
                "tup_deleted": float(db_stats["tup_deleted"] or 0),
                "temp_bytes": float(db_stats["temp_bytes"] or 0),
                "temp_files": float(db_stats["temp_files"] or 0),
                "buffers_checkpoint": int(bgwriter["buffers_checkpoint"] if bgwriter else 0),
                "buffers_clean": int(bgwriter["buffers_clean"] if bgwriter else 0),
                "buffers_backend": int(bgwriter["buffers_backend"] if bgwriter else 0),
                "buffers_alloc": int(bgwriter["buffers_alloc"] if bgwriter else 0),
            }
            state.update({
                "io_reads": metrics.get("_io_reads", 0),
                "io_writes": metrics.get("_io_writes", 0),
                "io_extends": metrics.get("_io_extends", 0),
            })
            metrics["_state"] = state

            return metrics
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

            has_kcache = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_kcache')"
            )

            sql = """
                SELECT
                    s.queryid::text,
                    LEFT(s.query, 2000) AS query,
                    s.calls,
                    s.total_exec_time AS total_time_ms,
                    s.mean_exec_time AS mean_time_ms,
                    s.rows,
                    s.shared_blks_hit,
                    s.shared_blks_read,
                    s.local_blks_hit,
                    s.local_blks_read,
                    s.temp_blks_read,
                    s.temp_blks_written
                FROM pg_stat_statements s
                WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            """
            if has_kcache:
                sql = """
                    SELECT
                        s.queryid::text,
                        LEFT(s.query, 2000) AS query,
                        s.calls,
                        s.total_exec_time AS total_time_ms,
                        s.mean_exec_time AS mean_time_ms,
                        s.rows,
                        s.shared_blks_hit,
                        s.shared_blks_read,
                        s.local_blks_hit,
                        s.local_blks_read,
                        s.temp_blks_read,
                        s.temp_blks_written,
                        k.plan_user_time,
                        k.plan_sys_time,
                        k.exec_user_time,
                        k.exec_sys_time
                    FROM pg_stat_statements s
                    LEFT JOIN pg_stat_kcache k ON k.queryid = s.queryid AND k.dbid = s.dbid
                    WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                """

            sql += " ORDER BY s.mean_exec_time DESC LIMIT $1"
            rows = await conn.fetch(sql, limit)
            return [dict(row) for row in rows]
        finally:
            await conn.close()
