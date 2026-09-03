"""Faz 16 İŞ 1 — ön koşul denetimi. Gerçek PostgreSQL/SQL Server olmadan (FakeAsyncConnection /
FakeSqlServerConnection ile), her kontrolün doğru durumu (ok/missing/unauthorized/unknown) ve
düzeltme komutunu ürettiğini kanıtlıyor — asıl amaç "neden hiç öneri gelmiyor?" sorusuna dbace'in
artık net bir cevap verebilmesi.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.collectors.base import ConnectionTarget
from app.domain.engines import DatabaseEngine
from app.services.prerequisites import (
    PrerequisiteCheck,
    check_postgresql_prerequisites,
    check_sqlserver_prerequisites,
    run_prerequisite_checks,
)
from tests.fakes import FakeAsyncConnection, FakeSqlServerConnection


def _pg_target() -> ConnectionTarget:
    return ConnectionTarget(host="pg.internal", port=5432, database="app", username="dbace", password="p")


def _sqlserver_target() -> ConnectionTarget:
    return ConnectionTarget(host="sql.internal", port=1433, database="master", username="sa", password="p")


def _pg_responses(**overrides) -> dict:
    base = {
        "-- ext:pg_stat_statements": True,
        "SHOW shared_preload_libraries": "pg_stat_statements,pg_cron",
        "SHOW pg_stat_statements.track": "top",
        "SELECT count(*) FROM pg_stat_statements": 42,
        "pg_has_role(current_user, 'pg_monitor'": True,
        "-- ext:hypopg": True,
        "-- ext:pg_qualstats": True,
        "-- ext:pg_buffercache": True,
        "SHOW track_io_timing": "on",
    }
    base.update(overrides)
    return base


async def _patch_asyncpg_connect(monkeypatch, conn: FakeAsyncConnection) -> None:
    import app.services.prerequisites as prereq_module

    async def fake_connect(**kwargs):
        return conn

    fake_asyncpg = types.SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    assert prereq_module  # module import already succeeded; local `import asyncpg` picks up the fake


def _by_key(checks: list[PrerequisiteCheck], key: str) -> PrerequisiteCheck:
    return next(c for c in checks if c.key == key)


async def test_postgresql_all_checks_ok(monkeypatch):
    conn = FakeAsyncConnection(_pg_responses())
    await _patch_asyncpg_connect(monkeypatch, conn)

    checks = await check_postgresql_prerequisites(_pg_target())

    assert checks  # non-empty
    assert all(c.status == "ok" for c in checks)


async def test_postgresql_missing_extension_has_high_severity_and_create_extension_fix(monkeypatch):
    conn = FakeAsyncConnection(_pg_responses(**{"-- ext:pg_stat_statements": False}))
    await _patch_asyncpg_connect(monkeypatch, conn)

    checks = await check_postgresql_prerequisites(_pg_target())

    pgss = _by_key(checks, "pg_stat_statements")
    assert pgss.status == "missing"
    assert pgss.severity == "high"
    assert "CREATE EXTENSION" in pgss.fix

    # Dependent checks (track, read permission) can't be evaluated without the extension.
    assert _by_key(checks, "pg_stat_statements_track").status == "unknown"
    assert _by_key(checks, "pg_stat_statements_read").status == "unknown"


async def test_postgresql_read_permission_denied_is_reported_as_unauthorized(monkeypatch):
    def _denied(_sql: str):
        raise RuntimeError('permission denied for view pg_stat_statements')

    conn = FakeAsyncConnection(_pg_responses(**{"SELECT count(*) FROM pg_stat_statements": _denied}))
    await _patch_asyncpg_connect(monkeypatch, conn)

    checks = await check_postgresql_prerequisites(_pg_target())

    read_check = _by_key(checks, "pg_stat_statements_read")
    assert read_check.status == "unauthorized"
    assert "GRANT" in read_check.fix


async def test_postgresql_optional_extensions_are_medium_severity_when_missing(monkeypatch):
    conn = FakeAsyncConnection(
        _pg_responses(**{"-- ext:hypopg": False, "-- ext:pg_qualstats": False, "-- ext:pg_buffercache": False})
    )
    await _patch_asyncpg_connect(monkeypatch, conn)

    checks = await check_postgresql_prerequisites(_pg_target())

    for key in ("hypopg", "pg_qualstats", "pg_buffercache"):
        check = _by_key(checks, key)
        assert check.status == "missing"
        assert check.severity == "medium"


async def test_postgresql_track_io_timing_off_flagged_for_io_diagnosis(monkeypatch):
    conn = FakeAsyncConnection(_pg_responses(**{"SHOW track_io_timing": "off"}))
    await _patch_asyncpg_connect(monkeypatch, conn)

    checks = await check_postgresql_prerequisites(_pg_target())

    check = _by_key(checks, "track_io_timing")
    assert check.status == "missing"
    assert "pg_reload_conf" in check.fix


def _sqlserver_responses(**overrides) -> dict:
    base = {
        "SET LOCK_TIMEOUT": ([], []),
        "HAS_PERMS_BY_NAME": ([(1,)], [("x",)]),
        "sys.dm_exec_query_stats": ([(b"hash",)], [("query_hash",)]),
        "sys.database_query_store_options": ([("READ_WRITE", "READ_WRITE")], [("actual", ""), ("desired", "")]),
    }
    base.update(overrides)
    return base


async def _patch_aioodbc_connect(monkeypatch, conn: FakeSqlServerConnection) -> None:
    async def fake_connect(**kwargs):
        return conn

    fake_aioodbc = types.SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "aioodbc", fake_aioodbc)


async def test_sqlserver_all_checks_ok(monkeypatch):
    conn = FakeSqlServerConnection(_sqlserver_responses())
    await _patch_aioodbc_connect(monkeypatch, conn)

    checks = await check_sqlserver_prerequisites(_sqlserver_target())

    assert checks
    assert all(c.status == "ok" for c in checks)


async def test_sqlserver_missing_view_server_state_permission(monkeypatch):
    conn = FakeSqlServerConnection(_sqlserver_responses(**{"HAS_PERMS_BY_NAME": ([(0,)], [("x",)])}))
    await _patch_aioodbc_connect(monkeypatch, conn)

    checks = await check_sqlserver_prerequisites(_sqlserver_target())

    check = _by_key(checks, "view_server_state")
    assert check.status == "missing"
    assert check.severity == "high"
    assert "GRANT VIEW SERVER STATE" in check.fix


async def test_sqlserver_query_store_off(monkeypatch):
    conn = FakeSqlServerConnection(
        _sqlserver_responses(**{"sys.database_query_store_options": ([("OFF", "OFF")], [("a", ""), ("d", "")])})
    )
    await _patch_aioodbc_connect(monkeypatch, conn)

    checks = await check_sqlserver_prerequisites(_sqlserver_target())

    check = _by_key(checks, "query_store")
    assert check.status == "missing"
    assert "QUERY_STORE = ON" in check.fix


async def test_sqlserver_dmv_permission_denied(monkeypatch):
    def _denied(_sql: str):
        raise RuntimeError("VIEW SERVER STATE permission was denied")

    conn = FakeSqlServerConnection(_sqlserver_responses(**{"sys.dm_exec_query_stats": _denied}))
    await _patch_aioodbc_connect(monkeypatch, conn)

    checks = await check_sqlserver_prerequisites(_sqlserver_target())

    check = _by_key(checks, "dmv_query_stats")
    assert check.status == "unauthorized"


async def test_run_prerequisite_checks_rejects_unsupported_engine():
    with pytest.raises(ValueError):
        await run_prerequisite_checks(DatabaseEngine.MONGODB, _pg_target())
