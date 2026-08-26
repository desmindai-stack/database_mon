"""Proves the wizard's one-shot group-creation endpoint (POST /api/wizard/database-groups) is
truly atomic: a failure partway through the node list (e.g. a duplicate server name) must roll
back everything already flushed in that request — the group, and any servers/instances/nodes
already created for earlier nodes in the same list — not leave a half-built group behind.
"""

from __future__ import annotations

import uuid

import httpx

from app.database import init_db
from app.main import app


async def _client() -> httpx.AsyncClient:
    await init_db()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_application(c: httpx.AsyncClient) -> dict:
    suffix = uuid.uuid4().hex[:8]
    cust = (await c.post("/api/customers", json={"name": f"wizard-test-{suffix}", "type": "public"})).json()
    return (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()


async def test_standalone_creates_group_server_instance_node_together():
    async with await _client() as c:
        application = await _make_application(c)
        r = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": application["id"],
                "group_name": "standalone-group",
                "engine": "postgresql",
                "topology": "standalone",
                "nodes": [
                    {
                        "server_name": "srv-1", "host": "srv-1.internal", "port": 5432,
                        "database": "postgres", "db_username": "postgres", "db_password": "x",
                    }
                ],
            },
        )
        assert r.status_code == 201, r.text
        group = r.json()

        nodes = (await c.get(f"/api/groups/{group['id']}/nodes")).json()
        assert len(nodes) == 1
        assert nodes[0]["instance_id"] is not None


async def test_duplicate_server_name_partway_through_rolls_back_everything():
    async with await _client() as c:
        application = await _make_application(c)

        # First: a lone server that will collide with the second node of the next (failing) call.
        pre_existing = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": application["id"],
                "group_name": "pre-existing-group",
                "engine": "postgresql",
                "topology": "standalone",
                "nodes": [
                    {
                        "server_name": "colliding-server", "host": "x.internal", "port": 5432,
                        "database": "postgres", "db_username": "postgres", "db_password": "x",
                    }
                ],
            },
        )
        assert pre_existing.status_code == 201

        customer_servers_before = await c.get(f"/api/servers?customer_id={application['customer_id']}")
        server_count_before = len(customer_servers_before.json())
        groups_before = (await c.get(f"/api/groups?application_id={application['id']}")).json()

        failing = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": application["id"],
                "group_name": "this-group-must-not-exist-after",
                "engine": "postgresql",
                "topology": "patroni",
                "access_name": "vip.internal",
                "cluster_name": "cn",
                "nodes": [
                    {
                        "server_name": "brand-new-server", "host": "new.internal", "port": 5432,
                        "database": "postgres", "db_username": "postgres", "db_password": "x",
                    },
                    {
                        # Collides with the server created in pre_existing above.
                        "server_name": "colliding-server", "host": "dup.internal", "port": 5432,
                        "database": "postgres", "db_username": "postgres", "db_password": "x",
                    },
                ],
            },
        )
        assert failing.status_code == 409, failing.text

        customer_servers_after = await c.get(f"/api/servers?customer_id={application['customer_id']}")
        assert len(customer_servers_after.json()) == server_count_before, (
            "the first node's server ('brand-new-server') leaked despite the transaction failing"
        )

        groups_after = (await c.get(f"/api/groups?application_id={application['id']}")).json()
        assert len(groups_after) == len(groups_before), "the group itself leaked despite the transaction failing"
        assert not any(g["name"] == "this-group-must-not-exist-after" for g in groups_after)


async def test_engine_topology_mismatch_rejected_before_anything_is_created():
    async with await _client() as c:
        application = await _make_application(c)
        groups_before = (await c.get(f"/api/groups?application_id={application['id']}")).json()

        r = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": application["id"],
                "group_name": "mismatched-group",
                "engine": "sqlserver",
                "topology": "patroni",  # patroni is postgresql-only
                "access_name": "vip.internal",
                "cluster_name": "cn",
                "nodes": [
                    {"server_name": "s1", "host": "s1.internal", "port": 1433, "db_username": "sa", "db_password": "x"},
                    {"server_name": "s2", "host": "s2.internal", "port": 1433, "db_username": "sa", "db_password": "x"},
                ],
            },
        )
        assert r.status_code == 422

        groups_after = (await c.get(f"/api/groups?application_id={application['id']}")).json()
        assert len(groups_after) == len(groups_before)
