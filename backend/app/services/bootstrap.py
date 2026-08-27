from __future__ import annotations

import logging
import secrets

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Customer, User
from app.services.security import hash_password

logger = logging.getLogger(__name__)


async def ensure_default_admin() -> None:
    """Runs on every boot, not just the first one — see SORULAR.md for the incident this fixes.

    - If no user named ADMIN_USERNAME exists yet: create one from ADMIN_USERNAME/ADMIN_PASSWORD.
      If ADMIN_PASSWORD isn't set, a random password is generated and logged once — safer than
      shipping a guessable default credential.
    - If that user exists but hasn't completed its forced first password change
      (must_change_password=True) and ADMIN_PASSWORD is set in .env: re-sync its password hash
      to the current .env value on every boot. This is what makes ".env'e ADMIN_PASSWORD
      eklemeyi unuttum, sonra ekledim" recoverable without a manual DB fix — previously the
      whole function short-circuited the moment the users table was non-empty, so a password
      added to .env *after* the admin's first (possibly randomly-generated) boot was silently
      ignored forever.
    - Once the user has actually changed their password (must_change_password=False), .env is
      never allowed to overwrite it again — a real self-chosen password always wins over .env.
    """
    async with SessionLocal() as session:
        existing_user = (
            await session.execute(select(User).where(User.username == settings.admin_username))
        ).scalar_one_or_none()

        if existing_user is None:
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
                logger.info("Admin oluşturuldu: %s", settings.admin_username)
            return

        if not existing_user.must_change_password:
            logger.info(
                "Admin mevcut, şifre değiştirilmiş, dokunulmadı: %s", settings.admin_username
            )
            return

        if settings.admin_password:
            existing_user.password_hash = hash_password(settings.admin_password)
            await session.commit()
            logger.info(
                "Admin şifresi .env'den güncellendi: %s (kullanıcı henüz ilk şifresini "
                "değiştirmemişti — bkz. SORULAR.md)",
                settings.admin_username,
            )
        else:
            logger.info(
                "Admin mevcut, ilk şifresini henüz değiştirmemiş ve .env'de ADMIN_PASSWORD "
                "tanımlı değil — mevcut şifreye dokunulmadı: %s. Kilitliyseniz "
                "scripts/reset_admin_password.py kullanın.",
                settings.admin_username,
            )


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
