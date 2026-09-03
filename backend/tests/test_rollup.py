"""Faz 16 İŞ 6 — günlük rollup: dünün MetricSample satırları tek bir MetricRollupDaily satırına
indirgeniyor mu, retention'ın süpürdüğü ham veriden bağımsız olarak. Şema taraması tarafı (tablo/
index boyutları) canlı bir bağlantı gerektirdiğinden burada monkeypatch'leniyor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import app.services.rollup as rollup_module
from app.database import SessionLocal, init_db
from app.models import Instance, MetricRollupDaily, MetricSample
from app.services.credentials import encrypt_secret


async def _make_instance() -> Instance:
    async with SessionLocal() as session:
        instance = Instance(
            name=f"rollup-test-{uuid.uuid4().hex[:8]}",
            engine="postgresql",
            host="rollup.internal",
            port=5432,
            database="postgres",
            username="postgres",
            password=encrypt_secret("x"),
            enabled=True,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance


async def test_daily_rollup_summarizes_yesterdays_metric_samples(monkeypatch):
    await init_db()

    async def no_schema_objects(session, instance, day):
        return 0

    monkeypatch.setattr(rollup_module, "_rollup_schema_objects_for_instance", no_schema_objects)

    instance = await _make_instance()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    window_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)

    async with SessionLocal() as session:
        for i, size in enumerate([100.0, 200.0, 300.0]):
            session.add(
                MetricSample(
                    instance_id=instance.id,
                    collected_at=window_start + timedelta(hours=i),
                    database_size_bytes=size,
                    active_connections=5 + i,
                )
            )
        await session.commit()

        counts = await rollup_module.run_daily_rollup(session)
        assert counts["metric_rows"] > 0

        rows = (
            await session.execute(
                select(MetricRollupDaily).where(
                    MetricRollupDaily.instance_id == instance.id,
                    MetricRollupDaily.metric_key == "database_size_bytes",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.day == yesterday
        assert row.min_value == 100.0
        assert row.max_value == 300.0
        assert row.avg_value == 200.0
        assert row.last_value == 300.0
        assert row.sample_count == 3


async def test_daily_rollup_is_idempotent_on_rerun(monkeypatch):
    await init_db()

    async def no_schema_objects(session, instance, day):
        return 0

    monkeypatch.setattr(rollup_module, "_rollup_schema_objects_for_instance", no_schema_objects)

    instance = await _make_instance()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    window_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)

    async with SessionLocal() as session:
        session.add(MetricSample(instance_id=instance.id, collected_at=window_start, database_size_bytes=500.0))
        await session.commit()

        await rollup_module.run_daily_rollup(session)
        await rollup_module.run_daily_rollup(session)

        rows = (
            await session.execute(
                select(MetricRollupDaily).where(
                    MetricRollupDaily.instance_id == instance.id,
                    MetricRollupDaily.metric_key == "database_size_bytes",
                )
            )
        ).scalars().all()
        assert len(rows) == 1  # upserted, not duplicated
