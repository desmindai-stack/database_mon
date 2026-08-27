"""Regression test for the admin-bootstrap incident: an admin created on a boot before
ADMIN_PASSWORD was ever added to .env kept its randomly-generated (and lost) password forever,
because ensure_default_admin() used to short-circuit the moment the users table was non-empty —
a password added to .env afterwards was silently ignored on every later boot.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal, init_db
from app.models import User
from app.services.bootstrap import ensure_default_admin
from app.services.security import verify_password


async def _get_user(username: str) -> User:
    async with SessionLocal() as session:
        return (await session.execute(select(User).where(User.username == username))).scalar_one()


async def test_first_boot_without_admin_password_generates_one(monkeypatch):
    await init_db()
    username = f"boot-admin-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "admin_username", username)
    monkeypatch.setattr(settings, "admin_password", None)

    await ensure_default_admin()

    user = await _get_user(username)
    assert user.role == "admin"
    assert user.must_change_password is True


async def test_first_boot_with_admin_password_uses_it(monkeypatch):
    await init_db()
    username = f"boot-admin-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "admin_username", username)
    monkeypatch.setattr(settings, "admin_password", "first-boot-pass-123")

    await ensure_default_admin()

    user = await _get_user(username)
    assert verify_password("first-boot-pass-123", user.password_hash)
    assert user.must_change_password is True


async def test_admin_password_added_to_env_after_first_boot_is_synced_on_reboot(monkeypatch):
    """The exact regression scenario: admin created with no ADMIN_PASSWORD (random, now-lost
    password), then ADMIN_PASSWORD is added to .env and the app restarts — login must work with
    the newly-set .env password, not stay locked to the forgotten random one."""
    await init_db()
    username = f"boot-admin-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "admin_username", username)

    # Boot 1: no ADMIN_PASSWORD set yet — random password generated (and, in real life, lost).
    monkeypatch.setattr(settings, "admin_password", None)
    await ensure_default_admin()
    user = await _get_user(username)
    assert user.must_change_password is True
    lost_password_hash = user.password_hash

    # Operator adds ADMIN_PASSWORD to .env and restarts — must_change_password is still True
    # (nobody ever logged in), so the fix should re-sync the password on this boot.
    monkeypatch.setattr(settings, "admin_password", "now-set-in-env-456")
    await ensure_default_admin()

    user = await _get_user(username)
    assert user.password_hash != lost_password_hash
    assert verify_password("now-set-in-env-456", user.password_hash)


async def test_self_changed_password_is_never_overwritten_by_env_again(monkeypatch):
    await init_db()
    username = f"boot-admin-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "admin_username", username)
    monkeypatch.setattr(settings, "admin_password", "original-env-pass")
    await ensure_default_admin()

    # User logs in and changes their password themselves — must_change_password flips to False
    # (mirrors what POST /api/auth/change-password does).
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        user.password_hash = "irrelevant-hash-just-needs-to-differ"
        user.must_change_password = False
        await session.commit()

    # .env still has (or now has a different) ADMIN_PASSWORD — must never win over a real,
    # user-chosen password again.
    monkeypatch.setattr(settings, "admin_password", "a-different-env-value")
    await ensure_default_admin()

    user = await _get_user(username)
    assert user.password_hash == "irrelevant-hash-just-needs-to-differ"


async def test_no_admin_password_set_leaves_pending_admin_untouched_without_crashing(monkeypatch):
    await init_db()
    username = f"boot-admin-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "admin_username", username)
    monkeypatch.setattr(settings, "admin_password", None)
    await ensure_default_admin()
    user = await _get_user(username)
    original_hash = user.password_hash

    # Reboot, still no ADMIN_PASSWORD in .env, still must_change_password=True — nothing to
    # sync from, must not crash and must not touch the existing (randomly-generated) password.
    await ensure_default_admin()

    user = await _get_user(username)
    assert user.password_hash == original_hash
