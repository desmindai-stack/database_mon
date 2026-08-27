"""Proves the auth system (Faz 15 İŞ 1): login, protected-by-default routes, the viewer
role's read-only restriction, refresh tokens, and the change-password flow.
"""

from __future__ import annotations

import uuid

import httpx

from app.database import SessionLocal, init_db
from app.main import app
from app.models import User
from app.services.security import hash_password


async def _raw_client() -> httpx.AsyncClient:
    await init_db()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_user(role: str, *, must_change_password: bool = False) -> tuple[str, str]:
    username = f"user-{uuid.uuid4().hex[:8]}"
    password = "correct-horse-battery-staple"
    async with SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
                must_change_password=must_change_password,
            )
        )
        await session.commit()
    return username, password


async def test_health_and_login_are_public():
    async with await _raw_client() as c:
        assert (await c.get("/api/health")).status_code == 200
        r = await c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        assert r.status_code == 401  # reachable without a token, correctly rejects bad creds


async def test_protected_endpoint_requires_token():
    async with await _raw_client() as c:
        r = await c.get("/api/customers")
        assert r.status_code == 401


async def test_login_success_and_me():
    username, password = await _make_user("admin")
    async with await _raw_client() as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["username"] == username
        assert body["user"]["role"] == "admin"
        assert "access_token" in body and "refresh_token" in body

        c.headers["Authorization"] = f"Bearer {body['access_token']}"
        me = await c.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == username


async def test_wrong_password_rejected():
    username, _ = await _make_user("admin")
    async with await _raw_client() as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": "not-it"})
        assert r.status_code == 401


async def test_viewer_can_read_but_not_write():
    username, password = await _make_user("viewer")
    async with await _raw_client() as c:
        login = await c.post("/api/auth/login", json={"username": username, "password": password})
        c.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        assert (await c.get("/api/customers")).status_code == 200
        create = await c.post("/api/customers", json={"name": f"viewer-blocked-{uuid.uuid4().hex[:6]}", "type": "public"})
        assert create.status_code == 403


async def test_admin_can_write():
    username, password = await _make_user("admin")
    async with await _raw_client() as c:
        login = await c.post("/api/auth/login", json={"username": username, "password": password})
        c.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        create = await c.post("/api/customers", json={"name": f"admin-allowed-{uuid.uuid4().hex[:6]}", "type": "public"})
        assert create.status_code == 201, create.text


async def test_refresh_token_issues_new_access_token():
    username, password = await _make_user("admin")
    async with await _raw_client() as c:
        login = await c.post("/api/auth/login", json={"username": username, "password": password})
        refresh_token = login.json()["refresh_token"]

        r = await c.post("/api/auth/refresh", json={"refresh_token": refresh_token})
        assert r.status_code == 200, r.text
        new_access = r.json()["access_token"]

        c.headers["Authorization"] = f"Bearer {new_access}"
        assert (await c.get("/api/auth/me")).status_code == 200


async def test_access_token_rejected_by_refresh_endpoint():
    """A refresh token and an access token aren't interchangeable — the `type` claim is
    checked, not just the signature."""
    username, password = await _make_user("admin")
    async with await _raw_client() as c:
        login = await c.post("/api/auth/login", json={"username": username, "password": password})
        access_token = login.json()["access_token"]

        r = await c.post("/api/auth/refresh", json={"refresh_token": access_token})
        assert r.status_code == 401


async def test_change_password_flow_clears_must_change_flag():
    username, password = await _make_user("admin", must_change_password=True)
    async with await _raw_client() as c:
        login = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert login.json()["user"]["must_change_password"] is True
        c.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        wrong = await c.post(
            "/api/auth/change-password", json={"current_password": "nope", "new_password": "brand-new-pass-123"}
        )
        assert wrong.status_code == 400

        ok = await c.post(
            "/api/auth/change-password",
            json={"current_password": password, "new_password": "brand-new-pass-123"},
        )
        assert ok.status_code == 200
        assert ok.json()["must_change_password"] is False

        # Old password no longer works, new one does.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c2:
            old_login = await c2.post("/api/auth/login", json={"username": username, "password": password})
            assert old_login.status_code == 401
            new_login = await c2.post(
                "/api/auth/login", json={"username": username, "password": "brand-new-pass-123"}
            )
            assert new_login.status_code == 200


async def test_inactive_user_cannot_login():
    username, password = await _make_user("admin")
    async with SessionLocal() as session:
        from sqlalchemy import select

        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        user.is_active = False
        await session.commit()

    async with await _raw_client() as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 403
