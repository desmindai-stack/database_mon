"""Faz 16 İŞ 6 — yeni kapasite planlama tahminleri (wraparound, tablo büyümesi, index şişmesi):
günlük rollup'lardan doğrusal regresyon + güven aralığıyla üretiliyor, yetersiz veri varsa hiç
üretilmiyor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal, init_db
from app.models import Instance, MetricRollupDaily, SchemaObjectDailySample
from app.services.credentials import encrypt_secret
from app.services.prediction import AUTOVACUUM_FREEZE_MAX_AGE, run_predictions
from tests.auth_helper import authed_client


async def _make_instance(session, *, name: str, engine: str = "postgresql") -> Instance:
    name = f"{name}-{uuid.uuid4().hex[:8]}"
    instance = Instance(
        name=name, engine=engine, host=f"{name}.internal", port=5432, database="postgres",
        username="postgres", password=encrypt_secret("x"),
    )
    session.add(instance)
    await session.flush()
    return instance


async def _add_metric_rollup(session, instance_id: int, metric_key: str, values: list[float]) -> None:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=len(values))
    for i, value in enumerate(values):
        session.add(
            MetricRollupDaily(
                instance_id=instance_id, metric_key=metric_key, day=start + timedelta(days=i),
                avg_value=value, min_value=value, max_value=value, last_value=value, sample_count=1,
            )
        )
    await session.flush()


async def _add_schema_object_samples(
    session, instance_id: int, object_kind: str, schema_name: str, object_name: str, values: list[float]
) -> None:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=len(values))
    for i, value in enumerate(values):
        session.add(
            SchemaObjectDailySample(
                instance_id=instance_id, day=start + timedelta(days=i), object_kind=object_kind,
                schema_name=schema_name, object_name=object_name, size_bytes=value,
            )
        )
    await session.flush()


async def test_wraparound_prediction_fires_when_age_climbing_toward_freeze_max():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-txid-climb")
        # Climbing ~3M/day for 8 days, already at 150M — will cross 200M (AUTOVACUUM_FREEZE_MAX_AGE)
        # well within the 365-day cutoff.
        values = [150_000_000 + i * 3_000_000 for i in range(8)]
        await _add_metric_rollup(session, instance.id, "transaction_id_age", values)

        created = await run_predictions(
            session, instance.id, {"transaction_id_age": values[-1]}, engine="postgresql", sample_interval_seconds=3600
        )
        await session.commit()

        wraparound = [c for c in created if c.metric_key == "transaction_id_age"]
        assert len(wraparound) == 1
        insight = wraparound[0]
        assert str(AUTOVACUUM_FREEZE_MAX_AGE) in insight.message.replace(",", "")
        assert "freeze" in insight.message.lower()
        assert insight.lower_bound is not None
        assert "VACUUM" in insight.recommendation


async def test_wraparound_prediction_skipped_after_recent_freeze_reset():
    """A VACUUM FREEZE resets transaction_id_age to near zero — a big drop mid-series means the
    trend isn't real yet; must not fabricate a prediction from stale pre-reset growth."""
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-txid-reset")
        values = [180_000_000, 190_000_000, 195_000_000, 200_000_000, 5_000_000, 5_100_000, 5_200_000, 5_300_000]
        await _add_metric_rollup(session, instance.id, "transaction_id_age", values)

        created = await run_predictions(
            session, instance.id, {"transaction_id_age": values[-1]}, engine="postgresql", sample_interval_seconds=3600
        )
        await session.commit()

        assert [c for c in created if c.metric_key == "transaction_id_age"] == []


async def test_wraparound_prediction_only_for_postgresql():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-txid-sqlserver", engine="sqlserver")
        values = [150_000_000 + i * 3_000_000 for i in range(8)]
        await _add_metric_rollup(session, instance.id, "transaction_id_age", values)

        created = await run_predictions(
            session, instance.id, {}, engine="sqlserver", sample_interval_seconds=3600
        )
        await session.commit()

        assert [c for c in created if c.metric_key == "transaction_id_age"] == []


async def test_table_growth_prediction_fires_for_growing_table():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-table-growth")
        values = [1_000_000.0 * (1 + 0.1 * i) for i in range(8)]
        await _add_schema_object_samples(session, instance.id, "table", "public", "events", values)

        created = await run_predictions(session, instance.id, {}, engine="postgresql", sample_interval_seconds=3600)
        await session.commit()

        table_insights = [c for c in created if c.metric_key.startswith("table_growth:")]
        assert len(table_insights) == 1
        assert "public.events" in table_insights[0].message
        assert table_insights[0].lower_bound is not None


async def test_index_bloat_prediction_fires_for_growing_unused_index():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-index-bloat")
        values = [500_000.0 * (1 + 0.15 * i) for i in range(8)]
        await _add_schema_object_samples(session, instance.id, "index", "public", "orders.idx_orders_old", values)

        created = await run_predictions(session, instance.id, {}, engine="postgresql", sample_interval_seconds=3600)
        await session.commit()

        index_insights = [c for c in created if c.metric_key.startswith("index_bloat:")]
        assert len(index_insights) == 1
        assert "REINDEX" in index_insights[0].recommendation or "kaldır" in index_insights[0].recommendation.lower()


async def test_table_growth_skipped_with_insufficient_days():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-table-insufficient")
        values = [1_000_000.0, 1_100_000.0]
        await _add_schema_object_samples(session, instance.id, "table", "public", "events", values)

        created = await run_predictions(session, instance.id, {}, engine="postgresql", sample_interval_seconds=3600)
        await session.commit()

        assert [c for c in created if c.metric_key.startswith("table_growth:")] == []


async def test_prediction_readiness_endpoint_lists_all_five_kinds_for_postgresql():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-readiness")
        await _add_metric_rollup(session, instance.id, "database_size_bytes", [1.0] * 3)
        instance_id = instance.id
        await session.commit()

    async with await authed_client() as c:
        resp = await c.get(f"/api/instances/{instance_id}/prediction-readiness")
        assert resp.status_code == 200
        body = resp.json()
        kinds = {r["kind"] for r in body}
        assert kinds == {"database_size", "connection_trend", "wraparound", "table_growth", "index_bloat"}
        db_size = next(r for r in body if r["kind"] == "database_size")
        assert db_size["have_days"] == 3
        assert db_size["ready"] is False
        assert db_size["days_remaining"] == 4.0


async def test_prediction_readiness_endpoint_omits_postgresql_only_kinds_for_sqlserver():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-readiness-mssql", engine="sqlserver")
        instance_id = instance.id
        await session.commit()

    async with await authed_client() as c:
        resp = await c.get(f"/api/instances/{instance_id}/prediction-readiness")
        assert resp.status_code == 200
        kinds = {r["kind"] for r in resp.json()}
        assert kinds == {"database_size", "connection_trend"}
