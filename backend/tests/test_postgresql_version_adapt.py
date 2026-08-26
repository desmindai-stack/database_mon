"""Version-adaptive behavior of PostgreSQLCollector — no real PostgreSQL server involved.

Reproduces the reported bug (PG17: "column checkpoints_timed does not exist" because
checkpoint stats moved from pg_stat_bgwriter to pg_stat_checkpointer) and proves the fix
picks the right view per detected server_version_num, degrades gracefully (per-metric,
without crashing the whole collect_metrics call) when a metric genuinely isn't available on
a version, and adapts the pg_stat_statements column rename (PG13+: total_exec_time/
mean_exec_time, pre-13: total_time/mean_time).
"""

from __future__ import annotations

import pytest

from app.collectors.base import ConnectionTarget
from app.collectors.postgresql import COLLECTOR_STATEMENT_TIMEOUT_MS, PostgreSQLCollector
from tests.fakes import FakeAsyncConnection

_DB_STATS_ROW = {
    "numbackends": 12,
    "xact_commit": 1000,
    "xact_rollback": 5,
    "blks_hit": 9000,
    "blks_read": 100,
    "deadlocks": 0,
    "temp_bytes": 0,
    "temp_files": 0,
    "tup_returned": 5000,
    "tup_fetched": 4000,
    "tup_inserted": 10,
    "tup_updated": 5,
    "tup_deleted": 1,
    "conflicts": 0,
}


def _base_responses(version_num: int, version_txt: str) -> dict:
    return {
        "current_setting('server_version_num')": {"num": version_num, "txt": version_txt},
        "FROM pg_stat_database": _DB_STATS_ROW,
        "SHOW max_connections": "100",
        "pg_database_size": 123_456,
        "pg_wal_lsn_diff": None,
    }


def _collector() -> PostgreSQLCollector:
    return PostgreSQLCollector(
        ConnectionTarget(host="db.internal", port=5432, database="postgres", username="u", password="p")
    )


async def test_pg17_uses_checkpointer_view_and_marks_buffers_backend_unsupported():
    """This is the exact reported bug: on PG17, the old pg_stat_bgwriter query for
    checkpoints_timed/etc. would raise "column does not exist" and crash the whole collector.
    The fix must use pg_stat_checkpointer instead and keep collecting everything else."""
    responses = _base_responses(170_000, "PostgreSQL 17.0 on x86_64-pc-linux-gnu")
    responses["FROM pg_stat_checkpointer"] = {
        "num_timed": 5,
        "num_requested": 2,
        "write_time": 123.4,
        "sync_time": 5.6,
        "buffers_written": 1000,
    }
    responses["FROM pg_stat_bgwriter"] = {"buffers_clean": 50, "buffers_alloc": 80}
    responses["FROM pg_stat_io"] = {"reads": 10, "writes": 20, "extends": 1, "op_bytes": 8192}
    conn = FakeAsyncConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)

    assert metrics["checkpoints_timed"] == 5
    assert metrics["checkpoints_req"] == 2
    assert metrics["checkpoint_write_time_ms"] == 123.4
    assert metrics["checkpoint_sync_time_ms"] == 5.6
    assert metrics["_server_version_num"] == 170_000
    assert metrics["_server_version"].startswith("PostgreSQL 17")

    # buffers_backend genuinely doesn't exist on PG17 anymore — must be absent from the
    # numeric metrics, not silently zero, and explained in _unsupported_metrics.
    assert "buffers_backend_per_sec" not in metrics
    assert "buffers_backend_per_sec" in metrics["_unsupported_metrics"]
    assert "17" in metrics["_unsupported_metrics"]["buffers_backend_per_sec"]

    # Other metrics collected fine — a checkpoint-stats surprise must not take down the rest.
    assert metrics["active_connections"] == 12
    assert metrics["database_size_bytes"] == 123_456
    assert "io_reads_per_sec" in metrics  # PG17 >= 16, pg_stat_io supported


async def test_pg16_uses_bgwriter_view_with_full_columns_and_supports_io():
    responses = _base_responses(160_000, "PostgreSQL 16.2 on x86_64-pc-linux-gnu")
    responses["FROM pg_stat_bgwriter"] = {
        "checkpoints_timed": 7,
        "checkpoints_req": 1,
        "checkpoint_write_time": 200.0,
        "checkpoint_sync_time": 10.0,
        "buffers_checkpoint": 500,
        "buffers_clean": 40,
        "buffers_backend": 30,
        "buffers_backend_fsync": 0,
        "buffers_alloc": 60,
    }
    responses["FROM pg_stat_io"] = {"reads": 1, "writes": 2, "extends": 0, "op_bytes": 8192}
    conn = FakeAsyncConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)

    assert metrics["checkpoints_timed"] == 7
    assert "buffers_backend_per_sec" in metrics  # still present pre-17
    assert metrics["_unsupported_metrics"] == {}
    assert "io_reads_per_sec" in metrics  # PG16 boundary is inclusive


async def test_pg12_falls_back_to_bgwriter_and_skips_pg_stat_io_with_reason():
    responses = _base_responses(120_005, "PostgreSQL 12.5 on x86_64-pc-linux-gnu")
    responses["FROM pg_stat_bgwriter"] = {
        "checkpoints_timed": 3,
        "checkpoints_req": 0,
        "checkpoint_write_time": 50.0,
        "checkpoint_sync_time": 1.0,
        "buffers_checkpoint": 100,
        "buffers_clean": 10,
        "buffers_backend": 5,
        "buffers_backend_fsync": 0,
        "buffers_alloc": 20,
    }
    conn = FakeAsyncConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)

    assert metrics["checkpoints_timed"] == 3  # bgwriter path still works pre-17
    assert "io_reads_per_sec" not in metrics
    assert not any("pg_stat_io" in q for q in conn.queries)  # query must not even be attempted
    reason = metrics["_unsupported_metrics"]["io_reads_per_sec"]
    assert "16" in reason


async def test_checkpoint_query_error_marks_unsupported_without_crashing_collect_metrics():
    def _boom(_sql: str):
        raise RuntimeError("permission denied for view pg_stat_checkpointer")

    responses = _base_responses(170_000, "PostgreSQL 17.0")
    responses["FROM pg_stat_checkpointer"] = _boom
    conn = FakeAsyncConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)  # must not raise

    assert "checkpoints_timed" not in metrics
    assert "permission denied" in metrics["_unsupported_metrics"]["checkpoints_timed"]
    assert metrics["active_connections"] == 12  # rest of collection still succeeded


@pytest.mark.parametrize(
    "version_num,expected_total_col",
    [(120_000, "s.total_time AS"), (130_000, "s.total_exec_time AS"), (170_000, "s.total_exec_time AS")],
)
async def test_slow_queries_uses_version_correct_pg_stat_statements_columns(version_num, expected_total_col):
    responses = {
        "current_setting('server_version_num')": {"num": version_num, "txt": "PostgreSQL test"},
        "pg_extension WHERE extname = 'pg_stat_statements'": True,
        "pg_extension WHERE extname = 'pg_stat_kcache'": False,
        "FROM pg_stat_statements": [],
    }
    conn = FakeAsyncConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    rows = await collector.collect_slow_queries(limit=10)

    assert rows == []
    executed_sql = conn.queries[-1]
    assert expected_total_col in executed_sql


def _fake_connect(conn: FakeAsyncConnection):
    async def _inner():
        return conn

    return _inner


async def test_connect_applies_statement_timeout(monkeypatch):
    """Exercises the real _connect() body (not monkeypatched away) to prove the collector
    actually sends a statement_timeout — every collection-loop query must be bounded so a
    slow/locked target can't pile up connections at collect_interval_seconds cadence."""
    fake_conn = FakeAsyncConnection({})

    async def fake_asyncpg_connect(**kwargs):
        return fake_conn

    import app.collectors.postgresql as pg_module

    monkeypatch.setattr(pg_module.asyncpg, "connect", fake_asyncpg_connect)

    collector = _collector()
    conn = await collector._connect()

    assert conn is fake_conn
    assert any(f"statement_timeout = '{COLLECTOR_STATEMENT_TIMEOUT_MS}ms'" in q for q in fake_conn.queries)
