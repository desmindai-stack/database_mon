"""Shared helper for tests that drive the app over httpx.ASGITransport (Faz 15 İŞ 1 made every
/api/* route auth-protected) — creates a throwaway admin user with a known password, logs in,
and returns a client with the bearer token pre-attached so existing tests don't need to change
anything beyond how they obtain their client.
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.main import app
from app.models import User
from app.services.security import hash_password

_TEST_PASSWORD = "test-pass-not-for-prod-123"


async def authed_client() -> httpx.AsyncClient:
    await init_db()
    username = f"test-admin-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        existing = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if existing is None:
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(_TEST_PASSWORD),
                    role="admin",
                    is_active=True,
                    must_change_password=False,
                )
            )
            await session.commit()

    # A request outside of `async with` still flips httpx's internal client state to OPENED —
    # the caller wraps whatever we return in its own `async with ... as c:`, which requires
    # UNOPENED, so log in with a separate short-lived client and hand back a fresh one.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as login_client:
        resp = await login_client.post("/api/auth/login", json={"username": username, "password": _TEST_PASSWORD})
        resp.raise_for_status()
        token = resp.json()["access_token"]

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    )
