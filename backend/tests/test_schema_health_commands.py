"""Faz 16-B İŞ 5 — schema health'te üretilen SQL komutları.

Bildirilen sorun: "DROP INDEX komutu yarım/eksik üretiliyor". Komut aslında tamdı; frontend'de
CSS ile kırpılıyordu (düzeltmesi arayüz tarafında). Bu testler komutların gerçekten tam
olduğunu — şema adı dahil, noktalı virgülle biten, çalıştırılabilir — kalıcı olarak garanti
altına alıyor ve bloat/vacuum satırlarına yeni eklenen komutları doğruluyor.
"""

from __future__ import annotations

from app.collectors.base import ConnectionTarget
from app.collectors.postgresql import PostgreSQLCollector
from tests.fakes import FakeAsyncConnection


def _collector() -> PostgreSQLCollector:
    return PostgreSQLCollector(
        ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    )


def _responses(**over) -> dict:
    base = {
        "pg_get_indexdef": [
            {
                "schema_name": "app",
                "table_name": "orders",
                "index_name": "idx_orders_created",
                "index_bytes": 2_000_000_000,
                "idx_scan": 0,
                "idx_tup_read": 0,
                "idx_tup_fetch": 0,
                "index_def": "CREATE INDEX idx_orders_created ON app.orders (created_at)",
            }
        ],
        "dead_ratio_pct": [
            {
                "schema_name": "app",
                "table_name": "events",
                "live_tup": 1000,
                "dead_tup": 900,
                "dead_ratio_pct": 90.0,
                "table_bytes": 500_000,
                "last_vacuum": None,
                "last_autovacuum": None,
                "last_analyze": None,
                "last_autoanalyze": None,
                "freeze_age": 1000,
            }
        ],
        "lag_sec": [
            {
                "schema_name": "app",
                "table_name": "audit",
                "live_tup": 50_000,
                "dead_tup": 100,
                "last_autovacuum": None,
                "last_autoanalyze": None,
                "lag_sec": 90_000.0,
                "freeze_age": 250_000_000,
            }
        ],
    }
    base.update(over)
    return base


async def _run(responses: dict) -> dict:
    conn = FakeAsyncConnection(responses)
    collector = _collector()

    async def _connect():
        return conn

    collector._connect = _connect  # type: ignore[method-assign]
    return await collector.collect_schema_health(limit=10)


def _assert_runnable(sql: str) -> None:
    """Her üretilen komut tek başına kopyalanıp çalıştırılabilir olmalı."""
    statements = [line for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert statements, f"komut boş: {sql!r}"
    for statement in statements:
        assert statement.rstrip().endswith(";"), f"noktalı virgül yok: {statement!r}"


async def test_drop_index_command_is_complete_and_schema_qualified():
    data = await _run(_responses())

    idx = data["unused_indexes"][0]
    assert idx["drop_ddl"] == 'DROP INDEX CONCURRENTLY IF EXISTS "app"."idx_orders_created";'
    _assert_runnable(idx["drop_ddl"])


async def test_unused_index_gets_severity_so_the_filter_can_reach_it():
    data = await _run(_responses())

    # 2 GB boşa harcanan index → kritik
    assert data["unused_indexes"][0]["severity"] == "critical"


async def test_bloated_table_command_is_plain_vacuum_with_full_only_as_a_comment():
    data = await _run(_responses())

    ddl = data["bloated_tables"][0]["vacuum_ddl"]
    assert ddl.startswith('VACUUM (ANALYZE) "app"."events";')
    # VACUUM FULL tabloyu ACCESS EXCLUSIVE kilitler — çalıştırılabilir komut olarak DEĞİL,
    # yorum satırı olarak sunulmalı.
    assert "-- VACUUM FULL" in ddl
    _assert_runnable(ddl)


async def test_vacuum_lag_command_uses_freeze_when_age_is_high():
    data = await _run(_responses())

    ddl = data["vacuum_lag"][0]["vacuum_ddl"]
    assert ddl == 'VACUUM (FREEZE, ANALYZE) "app"."audit";'
    _assert_runnable(ddl)


async def test_vacuum_lag_command_is_plain_when_freeze_age_is_low():
    responses = _responses()
    responses["lag_sec"][0]["freeze_age"] = 1000
    data = await _run(responses)

    assert data["vacuum_lag"][0]["vacuum_ddl"] == 'VACUUM (ANALYZE) "app"."audit";'


async def test_every_generated_command_names_its_schema():
    """Şema adı olmadan komut yanlış tabloda çalışabilir — üç listede de zorunlu."""
    data = await _run(_responses())

    commands = (
        [i["drop_ddl"] for i in data["unused_indexes"]]
        + [t["vacuum_ddl"] for t in data["bloated_tables"]]
        + [t["vacuum_ddl"] for t in data["vacuum_lag"]]
    )
    assert commands
    for command in commands:
        assert '"app".' in command, command
