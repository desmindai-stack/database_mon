"""Proves collect_instance() opens exactly ONE connection to the target per collection cycle
(collect_metrics + collect_slow_queries sharing it) instead of one each — the N+1-style
connection overhead this was written to eliminate. Runs against a real (SQLite, in-memory-ish
via a temp file) app DB but a fake target-DB connection — no real PostgreSQL/network needed.
"""

from __future__ import annotations

import uuid

from app.collectors.postgresql import PostgreSQLCollector
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module
from app.services.credentials import encrypt_secret
from tests.fakes import FakeAsyncConnection

_DB_STATS_ROW = {
    "numbackends": 1, "xact_commit": 0, "xact_rollback": 0, "blks_hit": 0, "blks_read": 0,
    "deadlocks": 0, "temp_bytes": 0, "temp_files": 0, "tup_returned": 0, "tup_fetched": 0,
    "tup_inserted": 0, "tup_updated": 0, "tup_deleted": 0, "conflicts": 0,
}


async def test_collect_instance_opens_only_one_connection_for_postgresql():
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"conn-sharing-check-{uuid.uuid4().hex[:8]}", engine="postgresql", host="fake.internal",
            port=5432, database="postgres", username="postgres", password=encrypt_secret("x"),
        )
        session.add(instance)
        await session.flush()

        connect_calls = {"count": 0}
        fake_conn = FakeAsyncConnection({
            "current_setting('server_version_num')": {"num": 160_000, "txt": "PostgreSQL 16.0 test"},
            "FROM pg_stat_database": _DB_STATS_ROW,
            "SHOW max_connections": "100",
            "pg_database_size": 0,
            "pg_wal_lsn_diff": None,
            "age(datfrozenxid)": 1_000_000,
            "FROM pg_stat_bgwriter": {
                "checkpoints_timed": 0, "checkpoints_req": 0, "checkpoint_write_time": 0,
                "checkpoint_sync_time": 0, "buffers_checkpoint": 0, "buffers_clean": 0,
                "buffers_backend": 0, "buffers_backend_fsync": 0, "buffers_alloc": 0,
            },
            "FROM pg_stat_io": {"reads": 0, "writes": 0, "extends": 0, "op_bytes": 0},
            "pg_extension WHERE extname = 'pg_stat_statements'": False,
        })

        real_collector = PostgreSQLCollector(collection_module._target_for(instance))

        async def counting_connect():
            connect_calls["count"] += 1
            return fake_conn

        real_collector._connect = counting_connect

        original_get_collector = collection_module.get_collector
        collection_module.get_collector = lambda engine, target: real_collector
        try:
            await collection_module.collect_instance(instance, session)
        finally:
            collection_module.get_collector = original_get_collector

        await session.commit()

        assert connect_calls["count"] == 1, (
            f"expected exactly 1 connection per collection cycle, got {connect_calls['count']}"
        )
