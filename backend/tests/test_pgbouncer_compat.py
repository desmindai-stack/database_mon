"""Regression coverage for the PgBouncer/Supabase-pooler prepared-statement incident:

    asyncpg.exceptions.DuplicatePreparedStatementError: prepared statement
    "__asyncpg_stmt_21__" already exists

asyncpg caches and names its prepared statements by default; PgBouncer in transaction/statement
pool_mode can route consecutive statements on one client connection to different real backend
connections, so a later EXECUTE can hit a backend that never saw the matching PREPARE (or one
that already has a statement under that generated name from a different client). Every asyncpg
connection dbace opens — collector, activity, schema health, parameter audit, EXPLAIN, index
advisor, and dbace's own metadata DB — must disable the statement cache unconditionally.
"""

from __future__ import annotations

import app.collectors.postgresql as pg_module
import app.services.custom_alert_rules as custom_alert_rules_module
import app.services.explain_service as explain_service_module
import app.services.index_advisor as index_advisor_module
import app.services.parameter_audit as parameter_audit_module
from app.collectors.base import ConnectionTarget, classify_connection_error, detect_pooler, resolve_uses_pooler
from app.database import _engine_kwargs_for
from app.services.explain_service import PostgreSQLExplainService
from app.services.index_advisor import PostgreSQLIndexAdvisor
from tests.fakes import FakeAsyncConnection


class _KwargCapture:
    """Records the kwargs asyncpg.connect() was called with, without opening a real socket."""

    def __init__(self, conn: FakeAsyncConnection | None = None) -> None:
        self.conn = conn or FakeAsyncConnection({})
        self.kwargs: dict | None = None

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.conn


async def test_collector_connect_disables_statement_cache(monkeypatch):
    capture = _KwargCapture()
    monkeypatch.setattr(pg_module.asyncpg, "connect", capture)
    target = ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    collector = pg_module.PostgreSQLCollector(target)

    await collector._connect()

    assert capture.kwargs["statement_cache_size"] == 0


async def test_parameter_audit_connect_disables_statement_cache(monkeypatch):
    from app.models import Instance

    capture = _KwargCapture()
    monkeypatch.setattr(parameter_audit_module.asyncpg, "connect", capture)
    instance = Instance(
        name="pa-test", engine="postgresql", host="h", port=5432, database="d",
        username="u", password="plain:p",
    )
    node = type("N", (), {"instance": instance})()

    await parameter_audit_module._pg_connect(node)

    assert capture.kwargs["statement_cache_size"] == 0


async def test_index_advisor_connect_disables_statement_cache(monkeypatch):
    capture = _KwargCapture()
    monkeypatch.setattr(index_advisor_module.asyncpg, "connect", capture)
    target = ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    advisor = PostgreSQLIndexAdvisor(target)

    await advisor._connect()

    assert capture.kwargs["statement_cache_size"] == 0


async def test_explain_service_connect_disables_statement_cache(monkeypatch):
    capture = _KwargCapture()
    monkeypatch.setattr(explain_service_module.asyncpg, "connect", capture)
    target = ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    service = PostgreSQLExplainService(target)

    await service._connect()

    assert capture.kwargs["statement_cache_size"] == 0


async def test_custom_alert_rules_connect_disables_statement_cache(monkeypatch):
    from app.models import Instance

    class _FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class _FakeReadonlyConn(FakeAsyncConnection):
        def transaction(self, readonly: bool = False):
            return _FakeTransaction()

        async def fetchval(self, sql, *args, **kwargs):
            return 1

    captured: dict = {}

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return _FakeReadonlyConn({})

    import asyncpg

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    instance = Instance(
        name="car-test", engine="postgresql", host="h", port=5432, database="d",
        username="u", password="plain:p",
    )

    await custom_alert_rules_module._run_query(instance, "SELECT 1")

    assert captured["statement_cache_size"] == 0


def test_sqlalchemy_engine_disables_statement_cache_for_asyncpg_urls():
    kwargs = _engine_kwargs_for("postgresql+asyncpg://user:pass@pooler.supabase.com:6543/postgres")
    assert kwargs["connect_args"] == {"statement_cache_size": 0}


def test_sqlalchemy_engine_leaves_sqlite_untouched():
    kwargs = _engine_kwargs_for("sqlite+aiosqlite:///./data/dbace.db")
    assert "connect_args" not in kwargs


def test_detect_pooler_by_hostname():
    assert detect_pooler("aws-0-eu-central-1.pooler.supabase.com", 5432) is True
    assert detect_pooler("my-pgbouncer.internal", 5432) is True
    assert detect_pooler("db.internal-corp.local", 5432) is False


def test_detect_pooler_by_port():
    assert detect_pooler("db.example.com", 6543) is True
    assert detect_pooler("db.example.com", 6432) is True
    assert detect_pooler("db.example.com", 5432) is False


def test_resolve_uses_pooler_explicit_override_wins_both_ways():
    # Host/port scream "pooler" but the operator explicitly turned it off.
    assert resolve_uses_pooler({"uses_pooler": False}, "x.pooler.supabase.com", 6543) is False
    # A self-hosted PgBouncer on a plain hostname/port the heuristic can't guess.
    assert resolve_uses_pooler({"uses_pooler": True}, "db.internal-corp.local", 5432) is True


def test_resolve_uses_pooler_falls_back_to_detection_when_unset():
    assert resolve_uses_pooler(None, "x.pooler.supabase.com", 6543) is True
    assert resolve_uses_pooler({}, "db.internal-corp.local", 5432) is False


def test_classify_connection_error_translates_prepared_statement_error():
    exc = Exception('prepared statement "__asyncpg_stmt_21__" already exists')
    message = classify_connection_error(exc)
    assert "havuzlayıcı" in message.lower() or "pooler" in message.lower()
    assert "Pooler kullanılıyor" in message


def test_classify_connection_error_still_handles_plain_auth_failure():
    exc = Exception("password authentication failed for user \"u\"")
    message = classify_connection_error(exc)
    assert "Kimlik doğrulama" in message
