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
from app.collectors.sqlserver_mongodb import _QS_BASE_COLUMNS
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


async def test_slow_queries_asks_which_columns_exist_instead_of_guessing():
    """FAZ 29 İŞ 2c — "dene, patlarsa tekrar dene" yerine SÜTUNLARI SOR.

    SQL Server sütunları temiz sürüm sınırlarında değil, SP/CU ile ekliyor. Eski kod
    `total_rows` için bir yedek sorgu tutuyordu; sömürülen sütun sayısı arttıkça bu desen
    kombinatoryal hale gelirdi (her opsiyonel sütun için ayrı yedek sorgu). DMV'nin gerçek
    sütun listesini okumak tek round-trip ve hiçbir SP kombinasyonunda "invalid column name"
    riski bırakmıyor.
    """
    available_rows = [(c,) for c in _QS_BASE_COLUMNS]  # opsiyonellerin HİÇBİRİ yok
    responses = {
        "SERVERPROPERTY('ProductMajorVersion')": _VERSION_ROW,
        # `sys.all_columns` yalnızca sonda sorgusunda geçiyor; "dm_exec_query_stats"
        # anahtarı ana sorguya da uyardı (sahte bağlantı alt-dize eşleştiriyor).
        "sys.all_columns": (available_rows, [("name",)]),
        "qs.total_elapsed_time": (
            [("abc123", "SELECT 1", 10, 5.0, 0.5, 2.0, 100, 0, 0, None, None, None, None,
              None, None, None, None, None, None)],
            [("queryid",), ("query",), ("calls",), ("total_time_ms",), ("mean_time_ms",),
             ("cpu_time_ms",), ("logical_reads",), ("physical_reads",), ("logical_writes",),
             ("rows",), ("min_time_ms",), ("max_time_ms",), ("min_cpu_ms",), ("max_cpu_ms",),
             ("grant_kb",), ("used_grant_kb",), ("spills",), ("plan_generation_num",),
             ("last_execution_time",)],
        ),
    }
    conn = FakeSqlServerConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    rows = await collector.collect_slow_queries(limit=5)

    assert len(rows) == 1
    assert rows[0]["queryid"] == "abc123"
    # Olmayan sütunlar alan olarak VAR ama değeri None: tüketicinin sütun varlığı kontrolü
    # yapmasına gerek kalmıyor ve "ölçülmedi" ile "sıfır" ayrımı korunuyor.
    assert rows[0]["rows"] is None
    assert rows[0]["spills"] is None
    # Temel alanlar gerçek değerlerle geliyor ve toplam süre CPU DEĞİL.
    assert rows[0]["total_time_ms"] == 5.0
    assert rows[0]["cpu_time_ms"] == 2.0


async def test_slow_queries_returns_nothing_when_the_dmv_is_unusable():
    """Temel sütunlar yoksa bu bir DMV değil ya da yetki sorunu var. Sessizce boş dönmek
    "yavaş sorgu yok" izlenimi verirdi — ama burada dönülecek doğru bir şey de yok; önemli
    olan uydurma veri üretmemek ve sebebi loglamak."""
    responses = {
        "SERVERPROPERTY('ProductMajorVersion')": _VERSION_ROW,
        "sys.all_columns": ([("execution_count",)], [("name",)]),
    }
    conn = FakeSqlServerConnection(responses)
    collector = _collector()
    collector._connect = _fake_connect(conn)  # type: ignore[method-assign]

    assert await collector.collect_slow_queries(limit=5) == []
