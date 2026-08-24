from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Customer

logger = logging.getLogger(__name__)


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
