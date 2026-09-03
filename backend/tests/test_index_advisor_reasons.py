"""Faz 16 İŞ 4 — index_advisor artık boş sonuç yerine HANGİ sebeple öneremediğini
(no_advice_reasons) döndürüyor. Gerçek bir PostgreSQL olmadan, FakeAsyncConnection ile her dalı
tek tek kanıtlıyor."""

from __future__ import annotations

import app.services.index_advisor as index_advisor_module
from app.collectors.base import ConnectionTarget
from app.services.index_advisor import MIN_SAMPLE_CALLS, PostgreSQLIndexAdvisor
from tests.fakes import FakeAsyncConnection


def _advisor() -> PostgreSQLIndexAdvisor:
    return PostgreSQLIndexAdvisor(
        ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    )


async def _patch_asyncpg(monkeypatch, conn: FakeAsyncConnection) -> None:
    async def fake_connect(**kwargs):
        return conn

    monkeypatch.setattr(index_advisor_module.asyncpg, "connect", fake_connect)


async def test_empty_query_returns_no_advice_and_no_reasons(monkeypatch):
    conn = FakeAsyncConnection({})
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("")
    assert advice == []
    assert reasons == []


async def test_query_without_from_clause_reports_no_query_data(monkeypatch):
    conn = FakeAsyncConnection({"EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False})
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT 1")
    assert advice == []
    assert [r.code for r in reasons] == ["no_query_data"]


async def test_query_without_filter_columns_reports_no_filter_columns(monkeypatch):
    conn = FakeAsyncConnection({"EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False})
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT * FROM orders")
    assert advice == []
    assert "no_filter_columns" in [r.code for r in reasons]


async def test_low_calls_adds_insufficient_samples_reason(monkeypatch):
    conn = FakeAsyncConnection({"EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False})
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT * FROM orders", calls=2)
    codes = [r.code for r in reasons]
    assert "insufficient_samples" in codes
    reason = next(r for r in reasons if r.code == "insufficient_samples")
    assert "2" in reason.message
    assert str(MIN_SAMPLE_CALLS) in reason.message


async def test_table_not_found_is_reported(monkeypatch):
    responses = {
        "EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False,
        "FROM pg_tables WHERE schemaname": False,
    }
    conn = FakeAsyncConnection(responses)
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT * FROM orders WHERE customer_id = 1")
    assert advice == []
    assert [r.code for r in reasons] == ["table_not_found"]


async def test_already_indexed_is_reported_instead_of_duplicate_advice(monkeypatch):
    responses = {
        "EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False,
        "FROM pg_tables WHERE schemaname": True,
        "FROM pg_class c": {"row_count": 10000, "total_bytes": 1_000_000},
        "FROM pg_indexes": [
            {"indexname": "idx_orders_customer_id", "indexdef": "CREATE INDEX idx_orders_customer_id ON orders (customer_id)"}
        ],
    }
    conn = FakeAsyncConnection(responses)
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT * FROM orders WHERE customer_id = 1")
    assert advice == []
    assert [r.code for r in reasons] == ["already_indexed"]


async def test_successful_advice_returns_no_reasons(monkeypatch):
    responses = {
        "EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')": False,
        "FROM pg_tables WHERE schemaname": True,
        "FROM pg_class c": {"row_count": 10000, "total_bytes": 1_000_000},
        "FROM pg_indexes": [],
        "FROM pg_stats": None,
        "reltuples::bigint FROM pg_class c JOIN pg_namespace": 10000,
    }
    conn = FakeAsyncConnection(responses)
    await _patch_asyncpg(monkeypatch, conn)
    advice, reasons = await _advisor().advise("SELECT * FROM orders WHERE customer_id = 1", calls=50)
    assert len(advice) == 1
    assert advice[0].table_name == "orders"
    assert reasons == []
