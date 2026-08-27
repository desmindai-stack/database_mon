"""Proves the admin screen's backend (Faz 15 İŞ 2): retention settings + manual cleanup
trigger, and user management — all admin-only regardless of HTTP method (unlike the general
require_write_access rule, GET included).
"""

from __future__ import annotations

import uuid

from tests.auth_helper import authed_client


async def test_viewer_cannot_reach_admin_routes_even_for_get():
    async with await authed_client("viewer") as c:
        r = await c.get("/api/admin/retention")
        assert r.status_code == 403


async def test_retention_get_defaults_and_put_rejects_bad_value():
    # AppSetting is process-wide (not per-test) and the SQLite test file persists across
    # separate pytest invocations, so don't assume a pristine 30-day default — only that GET
    # returns *some* allowed value, and that PUT actually changes it.
    async with await authed_client() as c:
        r = await c.get("/api/admin/retention")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["retention_days"] in [7, 14, 30, 60, 90]
        assert body["options"] == [7, 14, 30, 60, 90]

        bad = await c.put("/api/admin/retention", json={"retention_days": 45})
        assert bad.status_code == 400

        target = 60 if body["retention_days"] != 60 else 90
        ok = await c.put("/api/admin/retention", json={"retention_days": target})
        assert ok.status_code == 200
        assert ok.json()["retention_days"] == target


async def test_retention_manual_run_records_last_run():
    async with await authed_client() as c:
        r = await c.post("/api/admin/retention/run")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["last_run_at"] is not None
        assert body["last_deleted_count"] is not None


async def test_user_crud_and_self_protection():
    async with await authed_client() as c:
        me = await c.get("/api/auth/me")
        my_id = me.json()["id"]

        username = f"managed-{uuid.uuid4().hex[:8]}"
        create = await c.post(
            "/api/admin/users", json={"username": username, "password": "temp-pass-12345", "role": "viewer"}
        )
        assert create.status_code == 201, create.text
        user = create.json()
        assert user["role"] == "viewer"
        assert user["must_change_password"] is True

        dup = await c.post(
            "/api/admin/users", json={"username": username, "password": "temp-pass-12345", "role": "viewer"}
        )
        assert dup.status_code == 409

        listed = await c.get("/api/admin/users")
        assert any(u["username"] == username for u in listed.json())

        promote = await c.patch(f"/api/admin/users/{user['id']}", json={"role": "admin"})
        assert promote.status_code == 200
        assert promote.json()["role"] == "admin"

        reset = await c.post(f"/api/admin/users/{user['id']}/reset-password")
        assert reset.status_code == 200
        assert len(reset.json()["temporary_password"]) > 0

        deleted = await c.delete(f"/api/admin/users/{user['id']}")
        assert deleted.status_code == 204

        # Self-protection: can't deactivate or delete your own account.
        self_deactivate = await c.patch(f"/api/admin/users/{my_id}", json={"is_active": False})
        assert self_deactivate.status_code == 400

        self_delete = await c.delete(f"/api/admin/users/{my_id}")
        assert self_delete.status_code == 400
