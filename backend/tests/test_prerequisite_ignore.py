"""Faz 16-B İŞ 6 — ön koşul kontrollerini yoksayma.

Ortamda hiç kullanılmayacak bir uzantı (ör. pg_buffercache) yüzünden ön koşul listesi sonsuza
kadar kırmızı kalıyordu. Artık kontrol instance bazında kalıcı olarak yoksayılabiliyor:
yüzdeden düşüyor, dashboard uyarısı üretmiyor, geri alınabiliyor — ama gerçek durumu GİZLEMİYOR.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx

from app.routers.instances import build_prerequisite_report
from app.services.prerequisites import PrerequisiteCheck
from app.services.slow_query_status import _STATUS_TO_PREREQUISITE
from tests.auth_helper import authed_client


def _check(key: str, status: str) -> PrerequisiteCheck:
    return PrerequisiteCheck(key=key, name=key, status=status, severity="high", impact="etki")


def test_ignored_check_is_excluded_from_completion_but_keeps_its_real_status():
    checks = [_check("a", "ok"), _check("b", "ok"), _check("pg_buffercache", "missing")]

    report = build_prerequisite_report("postgresql", checks, {"pg_buffercache"})

    assert report.completion_pct == 100, "kalan zorunlu kontroller tamamsa %100 görünmeli"
    assert report.ok_count == 2
    assert report.issue_count == 0
    assert report.ignored_count == 1
    # Yoksaymak kontrolü yeşile boyamaz — gerçek durum korunur.
    ignored = next(c for c in report.checks if c.key == "pg_buffercache")
    assert ignored.status == "missing"
    assert ignored.ignored is True


def test_completion_pct_without_any_ignored():
    checks = [_check("a", "ok"), _check("b", "missing"), _check("c", "ok"), _check("d", "missing")]

    report = build_prerequisite_report("postgresql", checks, set())

    assert report.completion_pct == 50
    assert report.ignored_count == 0


def test_all_ignored_counts_as_complete():
    """Kullanıcı hepsini yoksaydıysa "denetlenecek bir şey yok" demiş oluyor — %0 göstermek
    yanıltıcı olurdu."""
    checks = [_check("a", "missing"), _check("b", "missing")]

    report = build_prerequisite_report("postgresql", checks, {"a", "b"})

    assert report.completion_pct == 100
    assert report.issue_count == 0


def test_partial_status_counts_as_an_issue_when_not_ignored():
    checks = [_check("a", "ok"), _check("b", "partial")]

    assert build_prerequisite_report("postgresql", checks, set()).issue_count == 1
    assert build_prerequisite_report("postgresql", checks, {"b"}).issue_count == 0


def test_every_ignorable_slow_query_status_maps_to_a_real_check_key():
    """Yoksayılan kontrolün etkilediği özelliği bildirebilmek için eşleme doğru olmalı."""
    valid_keys = {
        "pg_stat_statements",
        "shared_preload_libraries",
        "pg_stat_statements_track",
        "pg_stat_statements_read",
        "pg_stat_statements_visibility",
    }
    assert set(_STATUS_TO_PREREQUISITE.values()) <= valid_keys


async def _make_instance(c: httpx.AsyncClient) -> dict:
    suffix = uuid.uuid4().hex[:8]
    r = await c.post(
        "/api/instances",
        json={
            "name": f"ign-{suffix}",
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


async def test_ignored_list_persists_and_can_be_reverted():
    async with await authed_client() as c:
        inst = await _make_instance(c)

        saved = await c.put(
            f"/api/instances/{inst['id']}/prerequisites/ignored",
            json={"keys": ["pg_qualstats", "pg_buffercache", "pg_buffercache"]},
        )
        assert saved.status_code == 200, saved.text
        # Tekilleştirilmiş ve sıralı saklanır.
        assert saved.json() == ["pg_buffercache", "pg_qualstats"]

        # Kalıcı: yeni bir istekte de duruyor.
        again = await c.put(
            f"/api/instances/{inst['id']}/prerequisites/ignored", json={"keys": ["pg_qualstats"]}
        )
        assert again.json() == ["pg_qualstats"]

        # Geri alınabilir.
        cleared = await c.put(f"/api/instances/{inst['id']}/prerequisites/ignored", json={"keys": []})
        assert cleared.json() == []


async def test_ignored_prerequisites_do_not_produce_dashboard_recommendations():
    import app.services.dashboard_snapshot as ds

    group = SimpleNamespace(id=1, name="g", engine="postgresql")
    instance = SimpleNamespace(
        id=42,
        host="h",
        port=5432,
        database="d",
        username="u",
        password="plain:p",
        options=None,
        ignored_prerequisites=["pg_buffercache"],
    )
    nodes = [SimpleNamespace(role_hint="primary", instance=instance)]

    async def fake_checks(engine, target):
        return [
            _check("pg_buffercache", "missing"),
            _check("pg_stat_statements", "missing"),
        ]

    original = ds.run_prerequisite_checks
    ds.run_prerequisite_checks = fake_checks  # type: ignore[assignment]
    try:
        recs = await ds._prerequisite_recommendations(group, nodes)
    finally:
        ds.run_prerequisite_checks = original  # type: ignore[assignment]

    assert len(recs) == 1
    assert "pg_stat_statements" in recs[0]["message"]
