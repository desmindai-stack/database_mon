"""Proves the dashboard issue/recommendation enrichment added for the Faz 15 İŞ 4 card
redesign — checked_at stamped on issues, recommendations carrying steps + their own
customer/application/environment/link_hint/checked_at (previously only group name) — without
depending on a live network probe (which real down_nodes detection would require).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.models import GroupHealthSnapshot
from app.services.dashboard import collect_dashboard_summary
from tests.auth_helper import authed_client


async def _isolate_snapshots(session, keep_group_id: int) -> None:
    """Bu testin grubunu `top_issues` listesinde garanti eder.

    `collect_dashboard_summary` en fazla 10 sorun döndürüyor ve test veritabanı ayrı pytest
    çalıştırmaları arasında KALICI. Zamanla biriken `GroupHealthSnapshot` satırları testin
    kendi grubunu ilk 10'un dışına itiyor ve `next(...)` StopIteration ile patlıyordu — kodla
    ilgisi olmayan, sıraya bağlı bir kırılganlık. Diğer grupların anlık görüntüleri siliniyor;
    bu tablo grup başına TEK satır tutan bir önbellek (geçmiş değil), yeniden üretilir.
    """
    await session.execute(delete(GroupHealthSnapshot).where(GroupHealthSnapshot.group_id != keep_group_id))
    await session.commit()


async def test_issue_and_recommendation_enrichment():
    await init_db()
    async with await authed_client() as c:
        suffix = uuid.uuid4().hex[:8]
        cust = (await c.post("/api/customers", json={"name": f"enrich-test-{suffix}", "type": "public"})).json()
        app = (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()
        group_name = f"enrich-standalone-{suffix}"
        group = (
            await c.post(
                "/api/wizard/database-groups",
                json={
                    "application_id": app["id"],
                    "group_name": group_name,
                    "engine": "postgresql",
                    "topology": "standalone",
                    "nodes": [
                        {"server_name": "enrich-pg1", "host": "enrich-pg1.internal", "port": 5432,
                         "database": "postgres", "db_username": "postgres", "db_password": "x"}
                    ],
                },
            )
        ).json()

    checked_at = datetime.now(UTC)
    async with SessionLocal() as session:
        snapshot = GroupHealthSnapshot(
            group_id=group["id"],
            overall="critical",
            report_json={"overall": "critical", "down_nodes": [{"node_name": "enrich-pg1", "site": "primary"}]},
            recommendations_json=[
                {
                    "severity": "high",
                    "source": "connectivity",
                    "group": group_name,
                    "message": "1 düğüme erişilemiyor",
                    "steps": ["Sunucunun ayakta olduğunu doğrulayın.", "Servis durumunu kontrol edin."],
                    "action": "journalctl -u postgresql -n 100",
                }
            ],
            checked_at=checked_at,
        )
        session.add(snapshot)
        await session.commit()
        await _isolate_snapshots(session, group["id"])

        summary = await collect_dashboard_summary(session)

    issue = next(i for i in summary["top_issues"] if i["group"] == group_name)
    assert issue["checked_at"] == checked_at
    assert issue["node"] == "enrich-pg1 (primary)"
    assert issue["recommendation"] is not None
    rec = issue["recommendation"]
    assert rec["steps"] == ["Sunucunun ayakta olduğunu doğrulayın.", "Servis durumunu kontrol edin."]
    assert rec["action"] == "journalctl -u postgresql -n 100"
    assert rec["customer"] == cust["name"]
    assert rec["application"] == app["name"]
    assert rec["link_hint"] == f"/groups/{group['id']}"
    assert rec["checked_at"] == checked_at

    top_rec = next(r for r in summary["recommendations"] if r["group"] == group_name)
    assert top_rec["steps"] == ["Sunucunun ayakta olduğunu doğrulayın.", "Servis durumunu kontrol edin."]
    assert top_rec["customer"] == cust["name"]

    # top_issues/recommendations are capped at 10 (highest severity first) — a leftover
    # "critical" snapshot from this test would otherwise permanently occupy a top-10 slot in
    # the shared/persistent test SQLite file and could crowd out a future run's own row.
    async with SessionLocal() as cleanup_session:
        stray = await cleanup_session.get(GroupHealthSnapshot, snapshot.id)
        if stray is not None:
            await cleanup_session.delete(stray)
            await cleanup_session.commit()


async def test_recommendation_own_link_hint_overrides_group_default():
    """Faz 16 İŞ 5: a recommendation that already knows its precise target (e.g. a specific
    instance's Tuning tab) must keep that link — the group page is only the fallback for
    sources that have nothing more specific to point at."""
    await init_db()
    async with await authed_client() as c:
        suffix = uuid.uuid4().hex[:8]
        cust = (await c.post("/api/customers", json={"name": f"linkhint-test-{suffix}", "type": "public"})).json()
        app = (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()
        group_name = f"linkhint-standalone-{suffix}"
        group = (
            await c.post(
                "/api/wizard/database-groups",
                json={
                    "application_id": app["id"],
                    "group_name": group_name,
                    "engine": "postgresql",
                    "topology": "standalone",
                    "nodes": [
                        {"server_name": "linkhint-pg1", "host": "linkhint-pg1.internal", "port": 5432,
                         "database": "postgres", "db_username": "postgres", "db_password": "x"}
                    ],
                },
            )
        ).json()

    checked_at = datetime.now(UTC)
    async with SessionLocal() as session:
        snapshot = GroupHealthSnapshot(
            group_id=group["id"],
            overall="warning",
            report_json={"overall": "warning"},
            recommendations_json=[
                {
                    "severity": "medium",
                    "source": "prerequisites",
                    "group": group_name,
                    "message": "Ön koşul eksik",
                    "steps": ["..."],
                    "action": "GRANT pg_monitor TO x;",
                    "link_hint": "/instances/999?tab=tuning",
                }
            ],
            checked_at=checked_at,
        )
        session.add(snapshot)
        await session.commit()
        await _isolate_snapshots(session, group["id"])

        summary = await collect_dashboard_summary(session)

    rec = next(r for r in summary["recommendations"] if r["group"] == group_name)
    assert rec["link_hint"] == "/instances/999?tab=tuning"

    async with SessionLocal() as cleanup_session:
        stray = await cleanup_session.get(GroupHealthSnapshot, snapshot.id)
        if stray is not None:
            await cleanup_session.delete(stray)
            await cleanup_session.commit()
