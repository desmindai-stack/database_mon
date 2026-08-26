"""Proves the wizard's "attach to an existing server" path (İŞ 3): WizardNodeInput.
existing_server_id reuses an already-registered Server row instead of creating a new one — the
scenario is a second named SQL Server instance on a box that already hosts one — and rejects a
server_id that belongs to a different customer.
"""

from __future__ import annotations

import uuid

import httpx

from app.database import init_db
from app.main import app


async def _client() -> httpx.AsyncClient:
    await init_db()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_application(c: httpx.AsyncClient, suffix: str) -> dict:
    cust = (await c.post("/api/customers", json={"name": f"existing-srv-test-{suffix}", "type": "public"})).json()
    return (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()


async def test_add_node_with_existing_server_id_reuses_server_row():
    async with await _client() as c:
        suffix = uuid.uuid4().hex[:8]
        application = await _make_application(c, suffix)

        r_create = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": application["id"],
                "group_name": "sqlsvr-ag",
                "engine": "sqlserver",
                "topology": "alwayson",
                "access_name": "listener.internal",
                "cluster_name": "ag1",
                "nodes": [
                    {"server_name": "winsvr-02", "host": "winsvr-02.internal", "port": 1433,
                     "database": "master", "db_username": "sa", "db_password": "x", "role_hint": "primary"},
                    {"server_name": "winsvr-03", "host": "winsvr-03.internal", "port": 1433,
                     "database": "master", "db_username": "sa", "db_password": "x", "role_hint": "replica"},
                ],
            },
        )
        assert r_create.status_code == 201, r_create.text
        ag_group = r_create.json()

        servers_before = (await c.get(f"/api/servers?customer_id={application['customer_id']}")).json()
        count_before = len(servers_before)
        winsvr_02_id = next(s["id"] for s in servers_before if s["name"] == "winsvr-02")

        r_add = await c.post(
            f"/api/wizard/groups/{ag_group['id']}/nodes",
            json={
                "nodes": [
                    {"existing_server_id": winsvr_02_id, "instance_name": "NAMEDINST", "port": 1434,
                     "database": "master", "db_username": "sa", "db_password": "x", "role_hint": "primary"},
                ]
            },
        )
        assert r_add.status_code == 201, r_add.text
        added = r_add.json()
        assert added[0]["server_id"] == winsvr_02_id
        assert added[0]["host"] == "winsvr-02.internal"

        servers_after = (await c.get(f"/api/servers?customer_id={application['customer_id']}")).json()
        assert len(servers_after) == count_before, "no new Server row should have been created"


async def test_existing_server_id_from_another_customer_rejected():
    async with await _client() as c:
        suffix = uuid.uuid4().hex[:8]
        app_a = await _make_application(c, f"a-{suffix}")
        app_b = await _make_application(c, f"b-{suffix}")

        r_a = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": app_a["id"], "group_name": "grp-a", "engine": "postgresql",
                "topology": "standalone",
                "nodes": [{"server_name": "a-server", "host": "a.internal", "port": 5432,
                           "database": "postgres", "db_username": "postgres", "db_password": "x"}],
            },
        )
        servers_a = (await c.get(f"/api/servers?customer_id={app_a['customer_id']}")).json()
        server_a_id = servers_a[0]["id"]

        r_b = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": app_b["id"], "group_name": "grp-b", "engine": "postgresql",
                "topology": "patroni", "access_name": "vip-b.internal", "cluster_name": "cn-b",
                "nodes": [
                    {"server_name": "b-server-1", "host": "b1.internal", "port": 5432,
                     "database": "postgres", "db_username": "postgres", "db_password": "x"},
                    {"server_name": "b-server-2", "host": "b2.internal", "port": 5432,
                     "database": "postgres", "db_username": "postgres", "db_password": "x"},
                ],
            },
        )
        group_b = r_b.json()

        r = await c.post(
            f"/api/wizard/groups/{group_b['id']}/nodes",
            json={"nodes": [{"existing_server_id": server_a_id, "port": 5432, "database": "postgres",
                             "db_username": "postgres", "db_password": "x"}]},
        )
        assert r.status_code == 404, r.text
