"""Faz 16-B İŞ 4 + Faz 18 İŞ 1 — "en sorunlu N sorgu" listesi.

Liste her zaman bir PENCERE içindeki değişime göre sıralanıyor. Faz 18 İŞ 1'de varsayılan
davranış değişti: aralık verilmediğinde artık "yalnızca son toplama döngüsü" değil, son 24
saatlik pencere kullanılıyor — rapor da aynı servisi kullandığı için ikisinin ayrışması
mümkün değil. Yanıt da düz liste değil zarflanmış (mod, pencere, filtrelenen sayısı).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from app.database import SessionLocal
from app.models import SlowQuerySample
from tests.auth_helper import authed_client


async def _make_instance(c: httpx.AsyncClient) -> dict:
    suffix = uuid.uuid4().hex[:8]
    r = await c.post(
        "/api/instances",
        json={
            "name": f"rank-{suffix}",
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


async def _seed(instance_id: int, rows: list[dict]) -> None:
    async with SessionLocal() as session:
        for row in rows:
            session.add(SlowQuerySample(instance_id=instance_id, **row))
        await session.commit()


def _at(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


async def test_default_view_uses_the_shared_window_and_sorts_by_requested_metric():
    """Aralık verilmediğinde varsayılan pencere (son 24 saat) kullanılır — raporla aynı."""
    async with await authed_client() as c:
        inst = await _make_instance(c)
        old_at, new_at = _at(120), _at(5)
        # Pencere içinde iki döngü: fark alınabilir.
        await _seed(
            inst["id"],
            [
                dict(collected_at=old_at, queryid="a", query="SELECT a", calls=0, total_time_ms=0, mean_time_ms=0),
                dict(collected_at=new_at, queryid="a", query="SELECT a", calls=10, total_time_ms=1000, mean_time_ms=100),
                dict(collected_at=old_at, queryid="b", query="SELECT b", calls=0, total_time_ms=0, mean_time_ms=0),
                dict(collected_at=new_at, queryid="b", query="SELECT b", calls=500, total_time_ms=2500, mean_time_ms=5),
                dict(collected_at=old_at, queryid="c", query="SELECT c", calls=0, total_time_ms=0, mean_time_ms=0),
                dict(collected_at=new_at, queryid="c", query="SELECT c", calls=2, total_time_ms=600, mean_time_ms=300),
            ],
        )

        body = (await c.get(f"/api/queries/{inst['id']}?sort=total&limit=2")).json()
        assert body["mode"] == "delta"
        assert body["window_start"] and body["window_end"]
        assert [r["queryid"] for r in body["items"]] == ["b", "a"]

        by_mean = (await c.get(f"/api/queries/{inst['id']}?sort=mean&limit=2")).json()
        assert [r["queryid"] for r in by_mean["items"]] == ["c", "a"]

        by_calls = (await c.get(f"/api/queries/{inst['id']}?sort=calls&limit=1")).json()
        assert [r["queryid"] for r in by_calls["items"]] == ["b"]


async def test_single_cycle_window_falls_back_to_snapshot_instead_of_empty_list():
    """Yeni eklenmiş bir instance'ta pencerede tek döngü vardır; fark alınamaz. Listeyi boş
    bırakmak yerine kümülatif değerler gösterilip mod açıkça bildiriliyor."""
    async with await authed_client() as c:
        inst = await _make_instance(c)
        await _seed(
            inst["id"],
            [dict(collected_at=_at(5), queryid="only", query="SELECT only", calls=3,
                  total_time_ms=900, mean_time_ms=300)],
        )

        body = (await c.get(f"/api/queries/{inst['id']}")).json()

        assert body["mode"] == "snapshot"
        assert [r["queryid"] for r in body["items"]] == ["only"]


async def test_range_mode_ranks_by_change_inside_the_window_not_cumulative_total():
    """Asıl düzeltme: kümülatif toplamı büyük ama pencerede hiç çalışmamış bir sorgu, pencerede
    çok çalışan bir sorgunun önüne geçmemeli."""
    async with await authed_client() as c:
        inst = await _make_instance(c)
        old, new = _at(30), _at(10)
        await _seed(
            inst["id"],
            [
                # "eski dev": kümülatif toplamı yüksek ama pencerede hiç değişmemiş
                dict(collected_at=old, queryid="giant", query="SELECT giant", calls=1000, total_time_ms=90_000, mean_time_ms=90),
                dict(collected_at=new, queryid="giant", query="SELECT giant", calls=1000, total_time_ms=90_000, mean_time_ms=90),
                # "yeni sorun": pencerede 5000 ms harcamış
                dict(collected_at=old, queryid="fresh", query="SELECT fresh", calls=10, total_time_ms=100, mean_time_ms=10),
                dict(collected_at=new, queryid="fresh", query="SELECT fresh", calls=110, total_time_ms=5100, mean_time_ms=46),
            ],
        )

        start = quote(_at(31).isoformat())
        end = quote(_at(5).isoformat())
        rows = (await c.get(f"/api/queries/{inst['id']}?sort=total&start={start}&end={end}")).json()["items"]

        assert [r["queryid"] for r in rows] == ["fresh"], "pencerede iş yapmayan sorgu listede olmamalı"
        assert rows[0]["total_time_ms"] == 5000
        assert rows[0]["calls"] == 100
        assert rows[0]["mean_time_ms"] == 50


async def test_range_mode_handles_counter_reset():
    """pg_stat_statements_reset() sonrası son değer ilkinden küçük olur; negatif fark üretmemeli."""
    async with await authed_client() as c:
        inst = await _make_instance(c)
        await _seed(
            inst["id"],
            [
                dict(collected_at=_at(30), queryid="q", query="SELECT q", calls=1000, total_time_ms=50_000, mean_time_ms=50),
                dict(collected_at=_at(10), queryid="q", query="SELECT q", calls=20, total_time_ms=400, mean_time_ms=20),
            ],
        )

        start = quote(_at(31).isoformat())
        rows = (await c.get(f"/api/queries/{inst['id']}?sort=total&start={start}")).json()["items"]

        assert len(rows) == 1
        assert rows[0]["total_time_ms"] == 400
        assert rows[0]["calls"] == 20


async def test_range_mode_drops_negligible_queries():
    async with await authed_client() as c:
        inst = await _make_instance(c)
        await _seed(
            inst["id"],
            [
                dict(collected_at=_at(30), queryid="tiny", query="SELECT tiny", calls=5, total_time_ms=100.0, mean_time_ms=20),
                dict(collected_at=_at(10), queryid="tiny", query="SELECT tiny", calls=6, total_time_ms=100.2, mean_time_ms=17),
                dict(collected_at=_at(30), queryid="real", query="SELECT real", calls=5, total_time_ms=100, mean_time_ms=20),
                dict(collected_at=_at(10), queryid="real", query="SELECT real", calls=25, total_time_ms=900, mean_time_ms=36),
            ],
        )

        start = quote(_at(31).isoformat())
        rows = (await c.get(f"/api/queries/{inst['id']}?sort=total&start={start}")).json()["items"]

        assert [r["queryid"] for r in rows] == ["real"]


async def test_invalid_sort_is_rejected():
    async with await authed_client() as c:
        inst = await _make_instance(c)
        r = await c.get(f"/api/queries/{inst['id']}?sort=bogus")
        assert r.status_code == 422
