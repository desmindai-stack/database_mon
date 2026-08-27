"""Proves GET /api/dashboard/summary's `groups` field (Faz 15 İŞ 3) — added so the dashboard's
clickable stat-card filter has something to show for "healthy"/"unknown" groups, which never
appear in top_issues (that list only ever contains groups with an active problem).
"""

from __future__ import annotations

import uuid

from tests.auth_helper import authed_client


async def test_dashboard_summary_lists_every_group_including_unknown():
    async with await authed_client() as c:
        suffix = uuid.uuid4().hex[:8]
        cust = (await c.post("/api/customers", json={"name": f"dash-test-{suffix}", "type": "public"})).json()
        app = (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()
        group = (
            await c.post(
                "/api/wizard/database-groups",
                json={
                    "application_id": app["id"],
                    "group_name": "dash-standalone",
                    "engine": "postgresql",
                    "topology": "standalone",
                    "nodes": [
                        {
                            "server_name": "dash-pg1",
                            "host": "dash-pg1.internal",
                            "port": 5432,
                            "database": "postgres",
                            "db_username": "postgres",
                            "db_password": "x",
                        }
                    ],
                },
            )
        ).json()

        summary = (await c.get("/api/dashboard/summary")).json()
        row = next((g for g in summary["groups"] if g["group_id"] == group["id"]), None)
        assert row is not None, "freshly created group must appear in the dashboard's groups list"
        # No GroupHealthSnapshot exists yet (no probe has run) — status must be "unknown", not
        # silently absent from the list the way top_issues would leave it.
        assert row["status"] == "unknown"
        assert row["group"] == "dash-standalone"
        assert row["link_hint"] == f"/groups/{group['id']}"
