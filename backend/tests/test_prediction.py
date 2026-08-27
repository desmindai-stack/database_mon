"""Proves the prediction service's actionable recommendations (Faz 15 İŞ 6): connection-growth
predictions carry an engine-appropriate max_connections/pooler hint, database-size growth
carries an estimated doubling-date message + archive/partition/vacuum recommendation, and a
flat/declining trend produces no prediction at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal, init_db
from app.models import Instance, MetricSample
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


async def test_database_growth_prediction_has_doubling_estimate_and_archive_recommendation():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-pg-growth")
        # Doubling roughly every few days at this rate — well within the 180-day cutoff.
        base = 10 * 1024 * 1024 * 1024  # 10 GB
        values = [base, base * 1.05, base * 1.10, base * 1.15, base * 1.20]
        await _add_samples(session, instance.id, "database_size_bytes", values, interval_seconds=3600)

        created = await run_predictions(
            session,
            instance.id,
            {"database_size_bytes": base * 1.25},
            engine="postgresql",
            sample_interval_seconds=3600,
        )
        await session.commit()

        assert len(created) == 1
        insight = created[0]
        assert insight.metric_key == "database_size_bytes"
        assert "iki katına" in insight.message
        assert insight.recommendation is not None
        assert "arşiv" in insight.recommendation.lower() or "partit" in insight.recommendation.lower()
        assert "vacuum" in insight.recommendation.lower()


async def test_flat_database_size_produces_no_prediction():
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, name="pred-pg-flat")
        base = 5 * 1024 * 1024 * 1024
        values = [base, base, base, base, base]
        await _add_samples(session, instance.id, "database_size_bytes", values, interval_seconds=3600)

        created = await run_predictions(
            session, instance.id, {"database_size_bytes": base}, engine="postgresql", sample_interval_seconds=3600
        )
        await session.commit()

        assert created == []
