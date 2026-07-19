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

    async def collect_activity(self, limit: int = 100) -> dict[str, Any]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                """
                SELECT
                    a.pid,
                    a.usename::text AS usename,
                    a.datname::text AS datname,
                    COALESCE(a.application_name, '') AS application_name,
                    host(a.client_addr) AS client_addr,
                    a.state::text AS state,
                    a.wait_event_type::text AS wait_event_type,
                    a.wait_event::text AS wait_event,
                    a.backend_type::text AS backend_type,
                    a.query_start,
                    a.state_change,
                    a.xact_start,
                    EXTRACT(EPOCH FROM (now() - a.query_start))::float AS query_duration_sec,
                    EXTRACT(EPOCH FROM (now() - a.xact_start))::float AS xact_duration_sec,
                    LEFT(a.query, 2000) AS query,
                    pg_blocking_pids(a.pid) AS blocking_pids
                FROM pg_stat_activity a
                WHERE a.pid <> pg_backend_pid()
                  AND a.backend_type = 'client backend'
                ORDER BY
                    CASE
                        WHEN cardinality(pg_blocking_pids(a.pid)) > 0 THEN 0
                        WHEN a.state = 'active' THEN 1
                        WHEN a.state = 'idle in transaction' THEN 2
                        ELSE 3
                    END,
                    a.query_start ASC NULLS LAST
                LIMIT $1
                """,
                limit,
            )

            sessions: list[dict[str, Any]] = []
            blocking: list[dict[str, Any]] = []
            totals = {
                "total": 0,
                "active": 0,
                "idle": 0,
                "idle_in_transaction": 0,
                "waiting": 0,
                "blocked": 0,
            }

            for row in rows:
                blocking_pids = list(row["blocking_pids"] or [])
                session = {
                    "pid": int(row["pid"]),
                    "usename": row["usename"],
                    "datname": row["datname"],
                    "application_name": row["application_name"] or "",
                    "client_addr": row["client_addr"],
                    "state": row["state"] or "unknown",
                    "wait_event_type": row["wait_event_type"],
                    "wait_event": row["wait_event"],
                    "backend_type": row["backend_type"],
                    "query_start": row["query_start"].isoformat() if row["query_start"] else None,
                    "state_change": row["state_change"].isoformat() if row["state_change"] else None,
                    "xact_start": row["xact_start"].isoformat() if row["xact_start"] else None,
                    "query_duration_sec": float(row["query_duration_sec"] or 0),
                    "xact_duration_sec": float(row["xact_duration_sec"] or 0) if row["xact_duration_sec"] is not None else None,
                    "query": row["query"] or "",
                    "blocking_pids": blocking_pids,
                    "blocked": len(blocking_pids) > 0,
                }
                sessions.append(session)

                totals["total"] += 1
                state = (session["state"] or "").lower()
                if state == "active":
                    totals["active"] += 1
                elif state == "idle":
                    totals["idle"] += 1
                elif "idle in transaction" in state:
                    totals["idle_in_transaction"] += 1
                if session["wait_event_type"]:
                    totals["waiting"] += 1
                if session["blocked"]:
                    totals["blocked"] += 1
                    for blocker in blocking_pids:
                        blocking.append(
                            {
                                "blocked_pid": session["pid"],
                                "blocking_pid": int(blocker),
                                "blocked_query": session["query"],
                                "wait_event_type": session["wait_event_type"],
                                "wait_event": session["wait_event"],
                                "duration_sec": session["query_duration_sec"],
                            }
                        )

            wait_rows = await conn.fetch(
                """
                SELECT
                    COALESCE(wait_event_type::text, 'None') AS wait_event_type,
                    COALESCE(wait_event::text, 'None') AS wait_event,
                    count(*)::int AS count
                FROM pg_stat_activity
                WHERE pid <> pg_backend_pid()
                  AND backend_type = 'client backend'
                  AND wait_event IS NOT NULL
                GROUP BY 1, 2
                ORDER BY count DESC
                LIMIT 20
                """
            )
            wait_events = [dict(r) for r in wait_rows]

            state_rows = await conn.fetch(
                """
                SELECT
                    COALESCE(state::text, 'unknown') AS state,
                    count(*)::int AS count
                FROM pg_stat_activity
                WHERE pid <> pg_backend_pid()
                  AND backend_type = 'client backend'
                GROUP BY 1
                ORDER BY count DESC
                """
            )
            state_summary = [dict(r) for r in state_rows]

            return {
                "sessions": sessions,
                "wait_events": wait_events,
                "state_summary": state_summary,
                "blocking": blocking,
                "totals": totals,
            }
        finally:
            await conn.close()

    async def collect_schema_health(self, limit: int = 50) -> dict[str, Any]:
        conn = await self._connect()
        try:
            unused_rows = await conn.fetch(
                """
                SELECT
                    n.nspname AS schema_name,
                    c.relname AS table_name,
                    i.relname AS index_name,
                    pg_relation_size(i.oid)::bigint AS index_bytes,
                    s.idx_scan::bigint AS idx_scan,
                    s.idx_tup_read::bigint AS idx_tup_read,
                    s.idx_tup_fetch::bigint AS idx_tup_fetch,
                    pg_get_indexdef(i.oid) AS index_def
                FROM pg_stat_user_indexes s
                JOIN pg_index x ON x.indexrelid = s.indexrelid
                JOIN pg_class i ON i.oid = s.indexrelid
                JOIN pg_class c ON c.oid = s.relid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE s.idx_scan = 0
                  AND NOT x.indisunique
                  AND NOT x.indisprimary
                  AND NOT x.indisexclusion
                  AND i.relkind = 'i'
                  AND pg_relation_size(i.oid) > 8192
                ORDER BY pg_relation_size(i.oid) DESC
                LIMIT $1
                """,
                limit,
            )
            unused_indexes = [
                {
                    "schema_name": r["schema_name"],
                    "table_name": r["table_name"],
                    "index_name": r["index_name"],
                    "index_bytes": int(r["index_bytes"] or 0),
                    "idx_scan": int(r["idx_scan"] or 0),
                    "idx_tup_read": int(r["idx_tup_read"] or 0),
                    "idx_tup_fetch": int(r["idx_tup_fetch"] or 0),
                    "index_def": r["index_def"] or "",
                    "drop_ddl": f'DROP INDEX CONCURRENTLY IF EXISTS "{r["schema_name"]}"."{r["index_name"]}";',
                }
                for r in unused_rows
            ]

            bloat_rows = await conn.fetch(
                """
                SELECT
                    n.nspname AS schema_name,
                    c.relname AS table_name,
                    s.n_live_tup::bigint AS live_tup,
                    s.n_dead_tup::bigint AS dead_tup,
                    CASE
                        WHEN s.n_live_tup > 0
                        THEN round((s.n_dead_tup::numeric / s.n_live_tup::numeric) * 100, 2)
                        ELSE 0
                    END AS dead_ratio_pct,
                    pg_table_size(c.oid)::bigint AS table_bytes,
                    s.last_vacuum,
                    s.last_autovacuum,
                    s.last_analyze,
                    s.last_autoanalyze,
                    age(c.relfrozenxid)::bigint AS freeze_age
                FROM pg_stat_user_tables s
                JOIN pg_class c ON c.oid = s.relid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE s.n_dead_tup >= 1000
                   OR (s.n_live_tup > 0 AND s.n_dead_tup::numeric / s.n_live_tup::numeric >= 0.1)
                ORDER BY s.n_dead_tup DESC
                LIMIT $1
                """,
                limit,
            )
            bloated_tables = [
                {
                    "schema_name": r["schema_name"],
                    "table_name": r["table_name"],
                    "live_tup": int(r["live_tup"] or 0),
                    "dead_tup": int(r["dead_tup"] or 0),
                    "dead_ratio_pct": float(r["dead_ratio_pct"] or 0),
                    "table_bytes": int(r["table_bytes"] or 0),
                    "last_vacuum": r["last_vacuum"].isoformat() if r["last_vacuum"] else None,
                    "last_autovacuum": r["last_autovacuum"].isoformat() if r["last_autovacuum"] else None,
                    "last_analyze": r["last_analyze"].isoformat() if r["last_analyze"] else None,
                    "last_autoanalyze": r["last_autoanalyze"].isoformat() if r["last_autoanalyze"] else None,
                    "freeze_age": int(r["freeze_age"] or 0),
                    "severity": (
                        "critical"
                        if float(r["dead_ratio_pct"] or 0) >= 40 or int(r["dead_tup"] or 0) >= 1_000_000
                        else "high"
                        if float(r["dead_ratio_pct"] or 0) >= 20 or int(r["dead_tup"] or 0) >= 100_000
                        else "medium"
                    ),
                }
                for r in bloat_rows
            ]

            vacuum_rows = await conn.fetch(
                """
                SELECT
                    n.nspname AS schema_name,
                    c.relname AS table_name,
                    s.n_live_tup::bigint AS live_tup,
                    s.n_dead_tup::bigint AS dead_tup,
                    s.last_autovacuum,
                    s.last_autoanalyze,
                    GREATEST(
                        COALESCE(EXTRACT(EPOCH FROM (now() - s.last_autovacuum)), 0),
                        COALESCE(EXTRACT(EPOCH FROM (now() - s.last_autoanalyze)), 0)
                    )::float AS lag_sec,
                    age(c.relfrozenxid)::bigint AS freeze_age
                FROM pg_stat_user_tables s
                JOIN pg_class c ON c.oid = s.relid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE s.n_live_tup > 1000
                  AND (
                    s.last_autovacuum IS NULL
                    OR s.last_autovacuum < now() - interval '24 hours'
                    OR age(c.relfrozenxid) > 100000000
                  )
                ORDER BY lag_sec DESC NULLS FIRST, freeze_age DESC
                LIMIT $1
                """,
                limit,
            )
            vacuum_lag = [
                {
                    "schema_name": r["schema_name"],
                    "table_name": r["table_name"],
                    "live_tup": int(r["live_tup"] or 0),
                    "dead_tup": int(r["dead_tup"] or 0),
                    "last_autovacuum": r["last_autovacuum"].isoformat() if r["last_autovacuum"] else None,
                    "last_autoanalyze": r["last_autoanalyze"].isoformat() if r["last_autoanalyze"] else None,
                    "lag_sec": float(r["lag_sec"] or 0),
                    "freeze_age": int(r["freeze_age"] or 0),
                    "severity": "critical" if int(r["freeze_age"] or 0) > 500_000_000 else "high" if r["last_autovacuum"] is None else "medium",
                }
                for r in vacuum_rows
            ]

            unused_bytes = sum(i["index_bytes"] for i in unused_indexes)
            return {
                "unused_indexes": unused_indexes,
                "bloated_tables": bloated_tables,
                "vacuum_lag": vacuum_lag,
                "totals": {
                    "unused_indexes": len(unused_indexes),
                    "unused_index_bytes": unused_bytes,
                    "bloated_tables": len(bloated_tables),
                    "vacuum_lag_tables": len(vacuum_lag),
                },
            }
        finally:
            await conn.close()
