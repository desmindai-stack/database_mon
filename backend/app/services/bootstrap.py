from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.models import Customer, User
from app.services.security import hash_password

logger = logging.getLogger(__name__)


async def ensure_default_admin() -> None:
    """First-boot only: if the users table is empty, create one admin from ADMIN_USERNAME/
    ADMIN_PASSWORD. If ADMIN_PASSWORD isn't set, a random password is generated and logged
    once — safer than shipping a guessable default credential (Faz 15 İŞ 1)."""
    async with SessionLocal() as session:
        existing = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        if existing:
            return
        password = settings.admin_password
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(12)
        session.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
                must_change_password=True,
            )
        )
        await session.commit()
        if generated:
            logger.warning(
                "ADMIN_PASSWORD not set — generated initial admin credentials: username=%s "
                "password=%s (shown once; a password change is required on first login).",
                settings.admin_username,
                password,
            )
        else:
            logger.info("Created initial admin user: %s", settings.admin_username)


async def ensure_default_customer() -> None:
    """Private deployments get exactly one auto-created customer, so the
    Applications flow has somewhere to attach to without a manual setup step."""
    if settings.deployment_mode != "private" or not settings.default_customer_name:
        return
    async with SessionLocal() as session:
        existing = (await session.execute(select(Customer))).scalars().first()
        if existing:
            return
        session.add(Customer(name=settings.default_customer_name, type="private"))
        await session.commit()
        logger.info("Created default private customer: %s", settings.default_customer_name)
