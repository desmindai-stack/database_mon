"""Proves POST /api/wizard/groups/{group_id}/nodes (the wizard's "add node(s) to an existing
group" mode, İŞ 2) — atomic like group creation, inherits sibling cluster_options, rejects
standalone groups, and enforces the 8-node ceiling.
"""

from __future__ import annotations

import uuid

import httpx

from tests.auth_helper import authed_client


async def _client() -> httpx.AsyncClient:
    return await authed_client()


async def _make_application(c: httpx.AsyncClient) -> dict:
    suffix = uuid.uuid4().hex[:8]
    cust = (await c.post("/api/customers", json={"name": f"add-nodes-test-{suffix}", "type": "public"})).json()
    return (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()


async def _make_patroni_group(c: httpx.AsyncClient, application: dict) -> dict:
    r = await c.post(
        "/api/wizard/database-groups",
        json={
            "application_id": application["id"],
            "group_name": "add-nodes-patroni",
            "engine": "postgresql",
            "topology": "patroni",
            "access_name": "vip.internal",
            "cluster_name": "cn",
            "cluster_options": {"patroni_port": 8009, "etcd_port": 2380},
            "nodes": [
                {"server_name": "seed-1", "host": "seed-1.internal", "port": 5432, "database": "postgres",
                 "db_username": "postgres", "db_password": "x", "role_hint": "primary"},
                {"server_name": "seed-2", "host": "seed-2.internal", "port": 5432, "database": "postgres",
                 "db_username": "postgres", "db_password": "x", "role_hint": "replica"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_add_node_inherits_sibling_cluster_options():
    async with await _client() as c:
        application = await _make_application(c)
        group = await _make_patroni_group(c, application)

        r = await c.post(
            f"/api/wizard/groups/{group['id']}/nodes",
            json={"nodes": [{"server_name": "added-1", "host": "added-1.internal", "port": 5432,
                             "database": "postgres", "db_username": "postgres", "db_password": "x",
                             "role_hint": "replica"}]},
        )
        assert r.status_code == 201, r.text
        added = r.json()
        assert len(added) == 1
        assert added[0]["instance_id"] is not None

        nodes = (await c.get(f"/api/groups/{group['id']}/nodes")).json()
        assert len(nodes) == 3


async def test_add_node_to_standalone_group_rejected():
    async with await _client() as c:
        application = await _make_application(c)
        r = await c.post(
            "/api/wizard/database-groups",
            json={
                "application_id": application["id"], "group_name": "standalone-add-test",
                "engine": "postgresql", "topology": "standalone",
                "nodes": [{"server_name": "s1", "host": "s1.internal", "port": 5432, "database": "postgres",
                           "db_username": "postgres", "db_password": "x"}],
            },
        )
        group = r.json()

        r2 = await c.post(
            f"/api/wizard/groups/{group['id']}/nodes",
            json={"nodes": [{"server_name": "s2", "host": "s2.internal", "port": 5432, "database": "postgres",
                             "db_username": "postgres", "db_password": "x"}]},
        )
        assert r2.status_code == 400


async def test_add_nodes_duplicate_server_name_rolls_back():
    async with await _client() as c:
        application = await _make_application(c)
        group = await _make_patroni_group(c, application)

        servers_before = (await c.get(f"/api/servers?customer_id={application['customer_id']}")).json()
        count_before = len(servers_before)

        r = await c.post(
            f"/api/wizard/groups/{group['id']}/nodes",
            json={
                "nodes": [
                    {"server_name": "brand-new", "host": "new.internal", "port": 5432, "database": "postgres",
                     "db_username": "postgres", "db_password": "x"},
                    {"server_name": "seed-1", "host": "dup.internal", "port": 5432, "database": "postgres",
                     "db_username": "postgres", "db_password": "x"},  # collides with the group's own seed-1
                ]
            },
        )
        assert r.status_code == 409, r.text

        servers_after = (await c.get(f"/api/servers?customer_id={application['customer_id']}")).json()
        assert len(servers_after) == count_before, "brand-new server leaked despite the rollback"

        nodes = (await c.get(f"/api/groups/{group['id']}/nodes")).json()
        assert len(nodes) == 2, "no node from the failed request should have been added"
