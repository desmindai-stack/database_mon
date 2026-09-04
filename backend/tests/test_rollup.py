"""Faz 16 İŞ 6 — günlük rollup: dünün MetricSample satırları tek bir MetricRollupDaily satırına
indirgeniyor mu, retention'ın süpürdüğü ham veriden bağımsız olarak. Şema taraması ve (Faz 17 İŞ 2
ile eklenen) parametre/ön koşul fotoğrafı canlı bağlantı gerektirdiğinden monkeypatch'leniyor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import app.services.rollup as rollup_module
from app.database import SessionLocal, init_db
from app.models import Instance, MetricRollupDaily, MetricSample
from app.services.credentials import encrypt_secret


import pytest


@pytest.fixture(autouse=True)
def _no_live_probes(monkeypatch):
    """Rollup canlı bağlantı açan üç probe içeriyor (şema taraması + parametre/ön koşul
    fotoğrafı). Testler gerçek bir sunucuya bağlanmamalı; her biri no-op'a çevriliyor."""

    async def _noop(*args, **kwargs):
        return 0

    monkeypatch.setattr(rollup_module, "_rollup_schema_objects_for_instance", _noop)
    monkeypatch.setattr(rollup_module, "_rollup_state_for_instance", _noop)


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


async def test_daily_rollup_summarizes_yesterdays_metric_samples():
    await init_db()
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


async def test_daily_rollup_is_idempotent_on_rerun():
    await init_db()
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
