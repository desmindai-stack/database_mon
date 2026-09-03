"""Faz 16-B İŞ 3 — metrik grafiklerinde özel zaman aralığı.

Hazır aralıklar (1/6/24 saat, 7 gün) `hours` ile geliyordu; grafikte sürükleyerek seçilen ya da
elle girilen bir pencere için `start`/`end` gerekiyor. Eski `hours` davranışı aynen korunuyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from app.database import SessionLocal
from app.models import MetricSample
from tests.auth_helper import authed_client


async def _make_instance(c: httpx.AsyncClient) -> dict:
    suffix = uuid.uuid4().hex[:8]
    r = await c.post(
        "/api/instances",
        json={
            "name": f"range-{suffix}",
            "engine": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "postgres",
            "username": "postgres",
            "password": "x",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed(instance_id: int, offsets_hours: list[float]) -> list[datetime]:
    stamps = []
    async with SessionLocal() as session:
        for offset in offsets_hours:
            at = datetime.now(UTC) - timedelta(hours=offset)
            session.add(MetricSample(instance_id=instance_id, collected_at=at, active_connections=int(offset)))
            stamps.append(at)
        await session.commit()
    return stamps


async def test_custom_range_returns_only_samples_inside_window():
    async with await authed_client() as c:
        inst = await _make_instance(c)
        await _seed(inst["id"], [10, 5, 1])

        start = quote((datetime.now(UTC) - timedelta(hours=6)).isoformat())
        end = quote((datetime.now(UTC) - timedelta(hours=2)).isoformat())
        rows = (await c.get(f"/api/metrics/{inst['id']}?start={start}&end={end}")).json()

        assert len(rows) == 1
        assert rows[0]["active_connections"] == 5


async def test_hours_still_works_when_no_custom_range_given():
    async with await authed_client() as c:
        inst = await _make_instance(c)
        await _seed(inst["id"], [10, 5, 1])

        rows = (await c.get(f"/api/metrics/{inst['id']}?hours=6")).json()

        assert sorted(r["active_connections"] for r in rows) == [1, 5]


async def test_start_without_end_is_open_ended():
    async with await authed_client() as c:
        inst = await _make_instance(c)
        await _seed(inst["id"], [10, 5, 1])

        start = quote((datetime.now(UTC) - timedelta(hours=7)).isoformat())
        rows = (await c.get(f"/api/metrics/{inst['id']}?start={start}")).json()

        assert sorted(r["active_connections"] for r in rows) == [1, 5]
