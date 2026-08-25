"""X Bank demo verisini SQLite'a ekler (elle çalıştırılır, otomatik tetiklenmez).

Kullanım (backend/ dizininden):
    python scripts/seed_demo.py

Idempotenttir: mevcut kayıtları isme göre bulup atlar, tekrar tekrar
çalıştırılabilir.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Application, Customer, DatabaseGroup, Node  # noqa: E402


async def _get_or_create_customer(session, name: str, ctype: str) -> Customer:
    existing = (await session.execute(select(Customer).where(Customer.name == name))).scalar_one_or_none()
    if existing:
        return existing
    customer = Customer(name=name, type=ctype)
    session.add(customer)
    await session.flush()
    return customer


async def _get_or_create_application(session, customer: Customer, name: str, description: str) -> Application:
    existing = (
        await session.execute(
            select(Application).where(Application.customer_id == customer.id, Application.name == name)
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    application = Application(customer_id=customer.id, name=name, description=description)
    session.add(application)
    await session.flush()
    return application


async def _get_or_create_group(
    session,
    application: Application,
    name: str,
    engine: str,
    topology: str,
    notes: str,
    environment: str = "prod",
    access_name: str | None = None,
) -> DatabaseGroup:
    existing = (
        await session.execute(
            select(DatabaseGroup).where(
                DatabaseGroup.application_id == application.id, DatabaseGroup.name == name
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    group = DatabaseGroup(
        application_id=application.id,
        name=name,
        engine=engine,
        topology=topology,
        environment=environment,
        access_name=access_name,
        notes=notes,
    )
    session.add(group)
    await session.flush()
    return group


async def _ensure_node(
    session,
    group: DatabaseGroup,
    name: str,
    host: str,
    port: int,
    site: str,
    role_hint: str,
) -> None:
    existing = (
        await session.execute(select(Node).where(Node.group_id == group.id, Node.name == name))
    ).scalar_one_or_none()
    if existing:
        return
    session.add(
        Node(
            group_id=group.id,
            name=name,
            host=host,
            port=port,
            site=site,
            role_hint=role_hint,
        )
    )


async def seed() -> None:
    await init_db()
    async with SessionLocal() as session:
        customer = await _get_or_create_customer(session, "X Bank", "private")

        boa = await _get_or_create_application(
            session, customer, "boa", "SQL Server Always On — 4 düğüm (1 disaster site)"
        )
        boa_group = await _get_or_create_group(
            session,
            boa,
            "boa-sqlserver-ag",
            engine="sqlserver",
            topology="alwayson",
            notes="4 düğüm Always On AG, düğüm 4 disaster site'ta.",
            environment="prod",
            access_name="boa-ag-listener.internal",
        )
        await _ensure_node(session, boa_group, "boa-node-1", "boa-node-1.internal", 1433, "primary", "primary")
        await _ensure_node(session, boa_group, "boa-node-2", "boa-node-2.internal", 1433, "primary", "replica")
        await _ensure_node(session, boa_group, "boa-node-3", "boa-node-3.internal", 1433, "primary", "replica")
        await _ensure_node(session, boa_group, "boa-node-4", "boa-node-4.dr.internal", 1433, "disaster", "replica")

        boa_test_group = await _get_or_create_group(
            session,
            boa,
            "boa-sqlserver-test",
            engine="sqlserver",
            topology="standalone",
            notes="Tek düğüm test ortamı.",
            environment="test",
        )
        await _ensure_node(
            session, boa_test_group, "boa-test-node-1", "boa-test-node-1.internal", 1433, "primary", "unknown"
        )

        aapara = await _get_or_create_application(
            session, customer, "aapara", "PostgreSQL Patroni — 3 düğüm (2 ana DC + 1 disaster site)"
        )
        aapara_group = await _get_or_create_group(
            session,
            aapara,
            "aapara-patroni",
            engine="postgresql",
            topology="patroni",
            notes="3 düğüm Patroni cluster, düğüm 3 disaster site'ta.",
            environment="prod",
            access_name="aapara-patroni-vip.internal",
        )
        await _ensure_node(
            session, aapara_group, "aapara-node-1", "aapara-node-1.internal", 5432, "primary", "primary"
        )
        await _ensure_node(
            session, aapara_group, "aapara-node-2", "aapara-node-2.internal", 5432, "primary", "replica"
        )
        await _ensure_node(
            session, aapara_group, "aapara-node-3", "aapara-node-3.dr.internal", 5432, "disaster", "replica"
        )

        aapara_test_group = await _get_or_create_group(
            session,
            aapara,
            "aapara-postgres-test",
            engine="postgresql",
            topology="standalone",
            notes="Tek düğüm test ortamı.",
            environment="test",
        )
        await _ensure_node(
            session, aapara_test_group, "aapara-test-node-1", "aapara-test-node-1.internal", 5432, "primary", "unknown"
        )

        await session.commit()
    print("Seed tamamlandı: X Bank (boa + aapara).")


if __name__ == "__main__":
    asyncio.run(seed())
