"""Proves the prediction service's actionable recommendations (Faz 15 İŞ 6): connection-growth
predictions carry an engine-appropriate max_connections/pooler hint, database-size growth
carries an estimated doubling-date message + archive/partition/vacuum recommendation, and a
flat/declining trend produces no prediction at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal, init_db
from app.models import Instance, MetricRollupDaily, MetricSample
from app.services.credentials import encrypt_secret
from app.services.prediction import run_predictions


async def _make_instance(session, *, name: str, engine: str = "postgresql") -> Instance:
    # Instance.name is globally unique and this test's SQLite file persists across separate
    # pytest invocations — a fixed name collides with a leftover row from an earlier run.
    name = f"{name}-{uuid.uuid4().hex[:8]}"
    instance = Instance(
        name=name,
        engine=engine,
        host=f"{name}.internal",
        port=5432,
        database="postgres",
        username="postgres",
        password=encrypt_secret("x"),
    )
    session.add(instance)
    await session.flush()
    return instance


async def _add_samples(session, instance_id: int, key: str, values: list[float], *, interval_seconds: int = 15) -> None:
    base = datetime.now(UTC) - timedelta(seconds=interval_seconds * len(values))
    for i, value in enumerate(values):
        session.add(
            MetricSample(
                instance_id=instance_id,
                collected_at=base + timedelta(seconds=interval_seconds * i),
                metrics_json={key: value},
            )
        )
    await session.flush()


async def test_connection_growth_prediction_has_engine_specific_recommendation():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-pg-conn", engine="postgresql")
        # Steadily climbing utilization — will breach the 85% default threshold.
        values = [60.0, 65.0, 70.0, 75.0, 80.0]
        await _add_samples(session, instance.id, "connection_utilization_pct", values)

        created = await run_predictions(
            session, instance.id, {"connection_utilization_pct": 82.0}, engine="postgresql", sample_interval_seconds=15
        )
        await session.commit()

        assert len(created) == 1
        insight = created[0]
        assert insight.metric_key == "connection_utilization_pct"
        assert insight.recommendation is not None
        assert "pooler" in insight.recommendation.lower() or "max_connections" in insight.recommendation.lower()
        assert insight.action == "SHOW max_connections;"


async def test_connection_growth_recommendation_differs_for_sqlserver():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-mssql-conn", engine="sqlserver")
        values = [60.0, 65.0, 70.0, 75.0, 80.0]
        await _add_samples(session, instance.id, "connection_utilization_pct", values)

        created = await run_predictions(
            session, instance.id, {"connection_utilization_pct": 82.0}, engine="sqlserver", sample_interval_seconds=15
        )
        await session.commit()

        assert len(created) == 1
        assert created[0].action is None  # no universal SQL Server equivalent of "SHOW"
        assert "havuz" in created[0].recommendation.lower()


async def _add_rollup(session, instance_id: int, metric_key: str, values_by_day: list[float]) -> None:
    """Faz 16 İŞ 6: database-size/wraparound/etc. predictions now read daily rollups, not raw
    samples — this seeds N days of MetricRollupDaily history directly (mirrors what
    services/rollup.py would have written)."""
    today = datetime.now(UTC).date()
    start = today - timedelta(days=len(values_by_day))
    for i, value in enumerate(values_by_day):
        session.add(
            MetricRollupDaily(
                instance_id=instance_id,
                metric_key=metric_key,
                day=start + timedelta(days=i),
                avg_value=value,
                min_value=value,
                max_value=value,
                last_value=value,
                sample_count=1,
            )
        )
    await session.flush()


async def test_database_growth_prediction_has_doubling_estimate_and_archive_recommendation():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-pg-growth")
        # A week of steady ~5%/day growth — well within the 180-day doubling cutoff.
        base = 10 * 1024 * 1024 * 1024  # 10 GB
        values = [base * (1 + 0.05 * i) for i in range(8)]
        await _add_rollup(session, instance.id, "database_size_bytes", values)

        created = await run_predictions(
            session, instance.id, {"database_size_bytes": values[-1]}, engine="postgresql", sample_interval_seconds=3600
        )
        await session.commit()

        db_size_insights = [c for c in created if c.metric_key == "database_size_bytes"]
        assert len(db_size_insights) == 1
        insight = db_size_insights[0]
        assert "iki katına" in insight.message
        assert insight.lower_bound is not None and insight.upper_bound is not None
        assert insight.recommendation is not None
        assert "arşiv" in insight.recommendation.lower() or "partit" in insight.recommendation.lower()
        assert "vacuum" in insight.recommendation.lower()


async def test_flat_database_size_produces_no_prediction():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-pg-flat")
        base = 5 * 1024 * 1024 * 1024
        values = [base] * 8
        await _add_rollup(session, instance.id, "database_size_bytes", values)

        created = await run_predictions(
            session, instance.id, {"database_size_bytes": base}, engine="postgresql", sample_interval_seconds=3600
        )
        await session.commit()

        assert [c for c in created if c.metric_key == "database_size_bytes"] == []


async def test_database_size_prediction_skipped_with_less_than_a_week_of_rollups():
    """Faz 16 İŞ 6: "yeterli veri yoksa tahmin uydurma" — 3 günlük rollup, 7 gün gerektiriyor."""
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-pg-insufficient")
        base = 10 * 1024 * 1024 * 1024
        values = [base, base * 1.1, base * 1.2]
        await _add_rollup(session, instance.id, "database_size_bytes", values)

        created = await run_predictions(
            session, instance.id, {"database_size_bytes": values[-1]}, engine="postgresql", sample_interval_seconds=3600
        )
        await session.commit()

        assert [c for c in created if c.metric_key == "database_size_bytes"] == []
