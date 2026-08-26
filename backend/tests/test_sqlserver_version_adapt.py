"""Version-adaptive behavior of SqlServerCollector — no real SQL Server or ODBC driver
involved (this sandbox has neither). Proves version detection (SERVERPROPERTY
ProductMajorVersion) works, and that DMV/perf-counter/column differences degrade gracefully
(per-metric, without crashing collect_metrics or losing all slow-query data) instead of
raising and taking the whole collector down.
"""

from __future__ import annotations

import sys
import types

from app.collectors.base import ConnectionTarget
from app.collectors.sqlserver_mongodb import COLLECTOR_LOCK_TIMEOUT_MS, SqlServerCollector
from tests.fakes import FakeSqlServerConnection

_VERSION_ROW = ([(13, "Microsoft SQL Server 2016 (SP3) - 13.0.6300.2")], [("major",), ("version",)])


def _base_responses() -> dict:
    return {
        "SERVERPROPERTY('ProductMajorVersion')": _VERSION_ROW,
        "sys.dm_exec_sessions WHERE is_user_process": ([(5,)], [("c",)]),
        "name = 'user connections'": ([(100,)], [("c",)]),
        "blocking_session_id <> 0": ([(1,)], [("c",)]),
        "FROM sys.database_files": ([(1_000_000,)], [("c",)]),
        "FROM tempdb.sys.database_files": ([(2_000_000,)], [("c",)]),
        "dm_os_performance_counters": (
            [
                ("Batch Requests/sec", 1000.0),
                ("Buffer cache hit ratio", 95.0),
                ("Buffer cache hit ratio base", 100.0),
                ("Number of Deadlocks/sec", 0.0),
            ],
            [("counter_name",), ("cntr_value",)],
        ),
    }


def _collector() -> SqlServerCollector:
    return SqlServerCollector(
        ConnectionTarget(host="sql.internal", port=1433, database="master", username="sa", password="p")
    )


def _fake_connect(conn: FakeSqlServerConnection):
    async def _inner():
        return conn

    return _inner


async def test_detects_version_and_collects_full_metrics_on_supported_server():
    conn = FakeSqlServerConnection(_base_responses())
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)

    assert metrics["_server_version_num"] == 13  # SQL Server 2016
    assert "2016" in metrics["_server_version"]
    assert metrics["active_connections"] == 5
    assert metrics["max_connections"] == 100
    assert metrics["blocked_sessions"] == 1
    assert metrics["cache_hit_ratio"] == 95.0
    assert metrics["database_size_bytes"] == 1_000_000
    assert metrics["_unsupported_metrics"] == {}


async def test_perf_counter_failure_marks_unsupported_without_crashing():
    def _boom(_sql: str):
        raise RuntimeError("counter instance not found (Azure SQL Database?)")

    responses = _base_responses()
    responses["dm_os_performance_counters"] = _boom
    conn = FakeSqlServerConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)  # must not raise

    for key in ("transactions_per_sec", "cache_hit_ratio", "deadlocks"):
        assert key not in metrics
        assert "counter instance not found" in metrics["_unsupported_metrics"][key]

    # Core session counts (a separate, unguarded query) still collected fine.
    assert metrics["active_connections"] == 5
    assert metrics["database_size_bytes"] == 1_000_000


async def test_tempdb_query_failure_marks_only_temp_bytes_unsupported():
    def _boom(_sql: str):
        raise RuntimeError("VIEW SERVER STATE permission denied in database 'tempdb'")

    responses = _base_responses()
    responses["FROM tempdb.sys.database_files"] = _boom
    conn = FakeSqlServerConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    metrics = await collector.collect_metrics(previous=None)

    assert "temp_bytes" not in metrics
    assert "permission denied" in metrics["_unsupported_metrics"]["temp_bytes"]
    assert metrics["cache_hit_ratio"] == 95.0  # unrelated metric unaffected


async def test_connect_applies_lock_timeout(monkeypatch):
    """Exercises the real _connect() body (not monkeypatched away) to prove the collector
    actually sends SET LOCK_TIMEOUT — SQL Server has no plain-SQL statement_timeout
    equivalent, LOCK_TIMEOUT is the closest portable protection (see SORULAR.md)."""
    responses = {"SET LOCK_TIMEOUT": ([], [])}
    fake_conn = FakeSqlServerConnection(responses)

    async def fake_aioodbc_connect(**kwargs):
        return fake_conn

    fake_aioodbc_module = types.SimpleNamespace(connect=fake_aioodbc_connect)
    monkeypatch.setitem(sys.modules, "aioodbc", fake_aioodbc_module)

    collector = _collector()
    conn = await collector._connect()

    assert conn is fake_conn
    assert any(f"SET LOCK_TIMEOUT {COLLECTOR_LOCK_TIMEOUT_MS}" in q for q in fake_conn.queries)


async def test_slow_queries_falls_back_when_total_rows_column_missing():
    def _boom(_sql: str):
        raise RuntimeError("Invalid column name 'total_rows'.")

    responses = {
        "SERVERPROPERTY('ProductMajorVersion')": _VERSION_ROW,
        "qs.total_rows AS rows": _boom,
        "NULL AS rows": (
            [("abc123", "SELECT 1", 10, 5.0, 0.5, None)],
            [("queryid",), ("query",), ("calls",), ("total_time_ms",), ("mean_time_ms",), ("rows",)],
        ),
    }
    conn = FakeSqlServerConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    rows = await collector.collect_slow_queries(limit=5)

    assert len(rows) == 1
    assert rows[0]["queryid"] == "abc123"
    assert rows[0]["rows"] is None  # degraded gracefully instead of losing the whole result
