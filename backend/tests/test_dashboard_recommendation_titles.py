"""Faz 16 İŞ 2 — every dashboard recommendation source must carry a short, non-empty `title`
("Öneri: <title>" heading) so a recommendation is never just plain prose lost in a longer
message. Exercises the four builder functions in dashboard_snapshot.py directly, without a live
DB connection (parameter_audit / prerequisite checks are monkeypatched)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.services import dashboard_snapshot as ds
from app.services.prerequisites import PrerequisiteCheck


def _group(engine: str = "postgresql") -> SimpleNamespace:
    return SimpleNamespace(id=1, name="grp", engine=engine)


def _node(instance=None, role_hint: str = "primary") -> SimpleNamespace:
    return SimpleNamespace(role_hint=role_hint, instance=instance)


def _instance() -> SimpleNamespace:
    return SimpleNamespace(
        id=42, host="h", port=5432, database="d", username="u", password="plain:p", options=None
    )


def test_connectivity_recommendation_has_title():
    group = _group()
    report = {"down_nodes": [{"node_name": "n1", "site": "primary"}]}
    recs = ds._connectivity_recommendations(group, report)
    assert recs
    assert recs[0]["title"]


async def test_parameter_recommendation_has_title(monkeypatch):
    group = _group()
    nodes = [_node(_instance())]

    async def fake_audit(g, n):
        return {
            "findings": [
                {"name": "shared_buffers", "severity": "high", "recommendation": "En az 256MB", "detail": "x"}
            ]
        }

    monkeypatch.setattr(ds, "collect_parameter_audit", fake_audit)
    recs = await ds._parameter_recommendations(group, nodes)
    assert recs
    assert recs[0]["title"]
    assert "shared_buffers" in recs[0]["title"]
    assert recs[0]["link_hint"] == "/groups/1?tab=parameters"


async def test_prerequisite_recommendation_has_title(monkeypatch):
    group = _group()
    nodes = [_node(_instance())]

    async def fake_checks(engine, target):
        return [
            PrerequisiteCheck(
                key="pg_monitor",
                name="pg_monitor rolü",
                status="missing",
                severity="high",
                impact="Aktivite ve yavaş sorgu listesi eksik dönebilir.",
                fix="GRANT pg_monitor TO x;",
            )
        ]

    monkeypatch.setattr(ds, "run_prerequisite_checks", fake_checks)
    recs = await ds._prerequisite_recommendations(group, nodes)
    assert recs
    assert recs[0]["title"]
    assert "pg_monitor" in recs[0]["title"]
    assert recs[0]["link_hint"] == "/instances/42?tab=tuning"


async def test_instance_recommendation_has_title():
    group = _group()
    snapshots = [
        {
            "instance_id": 42,
            "name": "inst1",
            "engine": "postgresql",
            "metrics_json": {"cache_hit_ratio": 50, "active_connections": 1, "max_connections": 100},
            # A real timestamp — collected_at=None would also trigger a separate "henüz metrik
            # yok" insight (action="metrics"), muddying this test's single-insight assertion.
            "collected_at": datetime.now(UTC),
        }
    ]
    recs = await ds._instance_recommendations(group, snapshots)
    assert recs
    assert all(r["title"] for r in recs)
    # Low cache_hit_ratio insight carries action="queries" — the link should land straight on
    # that tab, not the generic Tuning tab (Faz 16 İŞ 5: land as close to the fix as possible).
    assert all(r["link_hint"] == "/instances/42?tab=queries" for r in recs)
