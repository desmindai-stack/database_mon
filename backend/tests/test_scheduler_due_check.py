"""Proves collect_all_instances() honors Instance.collect_interval_seconds per-instance —
an instance with a longer override must be skipped on ticks it isn't due on yet, while an
instance using the (shorter) app-wide default keeps running on schedule. No real target DB
involved — collect_instance() itself is replaced with a call recorder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import app.collectors.scheduler as scheduler_module
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module


async def _make_instance(session, collect_interval_seconds: int | None) -> Instance:
    instance = Instance(
        name=f"due-check-{uuid.uuid4().hex[:8]}", engine="postgresql", host="fake.internal",
        port=5432, database="postgres", username="postgres", password="x",
        collect_interval_seconds=collect_interval_seconds,
    )
    session.add(instance)
    await session.flush()
    return instance


async def test_instance_with_longer_override_is_skipped_when_not_due(monkeypatch):
    await init_db()
    async with SessionLocal() as session:
        default_interval_instance = await _make_instance(session, None)
        rarely_instance = await _make_instance(session, 3600)
        await session.commit()

        now = datetime.now(UTC)
        elapsed_20s_ago = now - timedelta(seconds=20)
        collection_module._previous_state[default_interval_instance.id] = {"collected_at": elapsed_20s_ago}
        collection_module._previous_state[rarely_instance.id] = {"collected_at": elapsed_20s_ago}

        collected_ids: list[int] = []

        async def fake_collect_instance(instance, session):
            collected_ids.append(instance.id)

        monkeypatch.setattr(scheduler_module, "collect_instance", fake_collect_instance)
        monkeypatch.setattr(scheduler_module.settings, "collect_interval_seconds", 15)

        await scheduler_module.collect_all_instances()

        # 20s elapsed >= 15s default -> due; 20s elapsed < 3600s override -> not due yet.
        assert default_interval_instance.id in collected_ids
        assert rarely_instance.id not in collected_ids


async def test_never_collected_instance_is_always_due(monkeypatch):
    await init_db()
    async with SessionLocal() as session:
        instance = await _make_instance(session, 3600)
        await session.commit()

        collection_module._previous_state.pop(instance.id, None)  # ensure no prior state

        collected_ids: list[int] = []

        async def fake_collect_instance(instance, session):
            collected_ids.append(instance.id)

        monkeypatch.setattr(scheduler_module, "collect_instance", fake_collect_instance)

        await scheduler_module.collect_all_instances()

        assert instance.id in collected_ids
