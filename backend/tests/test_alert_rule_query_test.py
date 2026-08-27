"""Proves POST /api/alerts/rules/test-query (Faz 15 İŞ 7's "sorguyu test et") — validation and
target-resolution paths that don't require an actual live database connection.
"""

from __future__ import annotations

import uuid

from tests.auth_helper import authed_client


async def test_requires_exactly_one_target():
    async with await authed_client() as c:
        neither = await c.post("/api/alerts/rules/test-query", json={"sql_query": "SELECT 1"})
        assert neither.status_code == 400

        both = await c.post(
            "/api/alerts/rules/test-query",
            json={"sql_query": "SELECT 1", "instance_id": 1, "group_id": 1},
        )
        assert both.status_code == 400


async def test_rejects_non_select_query():
    async with await authed_client() as c:
        cust = (await c.post("/api/customers", json={"name": f"query-test-{uuid.uuid4().hex[:8]}", "type": "public"})).json()
        app = (await c.post("/api/applications", json={"customer_id": cust["id"], "name": "app"})).json()
        group = (
            await c.post(
                "/api/wizard/database-groups",
                json={
                    "application_id": app["id"], "group_name": "query-test-standalone",
                    "engine": "postgresql", "topology": "standalone",
                    "nodes": [{"server_name": "qt-pg1", "host": "qt-pg1.internal", "port": 5432,
                               "database": "postgres", "db_username": "postgres", "db_password": "x"}],
                },
            )
        ).json()

        r = await c.post(
            "/api/alerts/rules/test-query",
            json={"sql_query": "DELETE FROM foo", "group_id": group["id"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "salt-okunur" in body["message"] or "SELECT" in body["message"]


async def test_unreachable_target_reports_failure_not_crash():
    async with await authed_client() as c:
        cust = (await c.post("/api/customers", json={"name": f"query-test2-{uuid.uuid4().hex[:8]}", "type": "public"})).json()
        app = (await c.post("/api/applications", json={"customer_id": cust["id"], "name": "app"})).json()
        group = (
            await c.post(
                "/api/wizard/database-groups",
                json={
                    "application_id": app["id"], "group_name": "query-test-unreachable",
                    "engine": "postgresql", "topology": "standalone",
                    "nodes": [{"server_name": "qt-pg2", "host": "qt-pg2-does-not-exist.invalid", "port": 5432,
                               "database": "postgres", "db_username": "postgres", "db_password": "x"}],
                },
            )
        ).json()

        r = await c.post(
            "/api/alerts/rules/test-query",
            json={"sql_query": "SELECT 1", "group_id": group["id"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["message"]
