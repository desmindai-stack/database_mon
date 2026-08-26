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
from app.domain.engines import DEFAULT_DATABASES, DatabaseEngine  # noqa: E402
from app.models import Application, Customer, DatabaseGroup, Instance, Node, Server  # noqa: E402
from app.services.credentials import encrypt_secret  # noqa: E402

_DEMO_USERNAME = {"postgresql": "postgres", "sqlserver": "sa", "mongodb": "admin"}
_ENGINE_SERVICE_NAME = {"postgresql": "postgresql", "sqlserver": "sqlserver", "mongodb": "mongodb"}


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
    listener_port: int | None = None,
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
        listener_port=listener_port,
        notes=notes,
    )
    session.add(group)
    await session.flush()
    return group


async def _get_or_create_server(
    session, customer: Customer, name: str, host: str, os: str, site: str = "primary", ip_address: str | None = None
) -> Server:
    existing = (
        await session.execute(select(Server).where(Server.customer_id == customer.id, Server.name == name))
    ).scalar_one_or_none()
    if existing:
        return existing
    server = Server(customer_id=customer.id, name=name, host=host, os=os, site=site, ip_address=ip_address)
    session.add(server)
    await session.flush()
    return server


async def _ensure_node(
    session,
    application: Application,
    customer: Customer,
    group: DatabaseGroup,
    server: Server,
    name: str,
    port: int,
    role_hint: str,
    instance_name: str | None = None,
) -> None:
    existing = (
        await session.execute(select(Node).where(Node.group_id == group.id, Node.name == name))
    ).scalar_one_or_none()
    if existing:
        return

    instance = Instance(
        name=f"{group.name}-{name}",
        engine=group.engine,
        host=server.host,
        port=port,
        database=DEFAULT_DATABASES.get(DatabaseEngine(group.engine), "postgres"),
        username=_DEMO_USERNAME.get(group.engine, "postgres"),
        password=encrypt_secret("demo-password"),
        customer_name=customer.name,
        environment=customer.type,
        application=application.name,
        cluster_name=group.name,
        role=role_hint if role_hint != "unknown" else None,
        services=[_ENGINE_SERVICE_NAME.get(group.engine, group.engine)],
        group_id=group.id,
        enabled=True,
    )
    session.add(instance)
    await session.flush()

    session.add(
        Node(
            group_id=group.id,
            server_id=server.id,
            name=name,
            instance_name=instance_name,
            port=port,
            role_hint=role_hint,
            instance_id=instance.id,
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
            listener_port=1433,
        )
        boa_srv_1 = await _get_or_create_server(
            session, customer, "boa-winsvr-01", "boa-node-1.internal", "windows", ip_address="10.10.1.11"
        )
        boa_srv_2 = await _get_or_create_server(
            session, customer, "boa-winsvr-02", "boa-node-2.internal", "windows", ip_address="10.10.1.12"
        )
        boa_srv_3 = await _get_or_create_server(session, customer, "boa-winsvr-03", "boa-node-3.internal", "windows")
        boa_srv_4 = await _get_or_create_server(
            session, customer, "boa-winsvr-04-dr", "boa-node-4.dr.internal", "windows", site="disaster"
        )
        await _ensure_node(session, boa, customer, boa_group, boa_srv_1, "boa-node-1", 1433, "primary", "MSSQLSERVER")
        await _ensure_node(session, boa, customer, boa_group, boa_srv_2, "boa-node-2", 1433, "replica", "MSSQLSERVER")
        await _ensure_node(session, boa, customer, boa_group, boa_srv_3, "boa-node-3", 1433, "replica", "MSSQLSERVER")
        await _ensure_node(session, boa, customer, boa_group, boa_srv_4, "boa-node-4", 1433, "replica", "MSSQLSERVER")

        # Windows sunucusunda iki ayrı named SQL Server instance örneği: aynı fiziksel/VM
        # sunucu (boa-shared-winsvr), biri MSSQLSERVER (varsayılan instance) biri SQLPROD02
        # (named instance) olmak üzere iki ayrı Node (instance) barındırıyor — ve bu ikisi
        # FARKLI Always On gruplarına üye. Bu, Server/Node modelinin "bir Windows sunucusundaki
        # instance'lar birbirinden bağımsız gruplara üye olabilir" iddiasını kanıtlayan senaryo
        # (İŞ 1'in yapısal gereksinimi — bkz. SORULAR.md Faz 9 İŞ 1). Her iki AG'nin de gerçekçi
        # olması için ikinci birer replika düğüm ayrı sunuculara eklendi.
        boa_shared_srv = await _get_or_create_server(
            session, customer, "boa-shared-winsvr", "boa-shared-winsvr.internal", "windows"
        )
        boa_test_group = await _get_or_create_group(
            session,
            boa,
            "boa-sqlserver-test-ag",
            engine="sqlserver",
            topology="alwayson",
            notes="2 düğüm Always On AG (test ortamı) — birincil düğüm, paylaşımlı Windows sunucusundaki varsayılan (MSSQLSERVER) instance.",
            environment="test",
            access_name="boa-test-ag-listener.internal",
        )
        boa_test_srv_2 = await _get_or_create_server(
            session, customer, "boa-test-winsvr-02", "boa-test-winsvr-02.internal", "windows"
        )
        await _ensure_node(
            session, boa, customer, boa_test_group, boa_shared_srv, "boa-test-node-1", 1433, "primary", "MSSQLSERVER"
        )
        await _ensure_node(
            session, boa, customer, boa_test_group, boa_test_srv_2, "boa-test-node-2", 1433, "replica", "MSSQLSERVER"
        )
        boa_reporting_group = await _get_or_create_group(
            session,
            boa,
            "boa-reporting-ag",
            engine="sqlserver",
            topology="alwayson",
            notes="2 düğüm Always On AG (raporlama) — birincil düğüm, aynı paylaşımlı Windows sunucusunda AYRI bir named instance (SQLPROD02); boa-sqlserver-test-ag'den bağımsız bir AG.",
            environment="dev",
            access_name="boa-reporting-ag-listener.internal",
        )
        boa_reporting_srv_2 = await _get_or_create_server(
            session, customer, "boa-reporting-winsvr-02", "boa-reporting-winsvr-02.internal", "windows"
        )
        await _ensure_node(
            session, boa, customer, boa_reporting_group, boa_shared_srv, "boa-reporting-node-1", 1434, "primary", "SQLPROD02"
        )
        await _ensure_node(
            session, boa, customer, boa_reporting_group, boa_reporting_srv_2, "boa-reporting-node-2", 1434, "replica", "SQLPROD02"
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
            listener_port=5000,
        )
        # Linux/PostgreSQL: bir sunucuda tek servis, yani 1 sunucu = 1 Node.
        aapara_srv_1 = await _get_or_create_server(
            session, customer, "aapara-node-1", "aapara-node-1.internal", "linux", ip_address="10.20.1.11"
        )
        aapara_srv_2 = await _get_or_create_server(
            session, customer, "aapara-node-2", "aapara-node-2.internal", "linux", ip_address="10.20.1.12"
        )
        aapara_srv_3 = await _get_or_create_server(
            session, customer, "aapara-node-3-dr", "aapara-node-3.dr.internal", "linux", site="disaster"
        )
        await _ensure_node(session, aapara, customer, aapara_group, aapara_srv_1, "aapara-node-1", 5432, "primary")
        await _ensure_node(session, aapara, customer, aapara_group, aapara_srv_2, "aapara-node-2", 5432, "replica")
        await _ensure_node(session, aapara, customer, aapara_group, aapara_srv_3, "aapara-node-3", 5432, "replica")

        aapara_test_group = await _get_or_create_group(
            session,
            aapara,
            "aapara-postgres-test",
            engine="postgresql",
            topology="standalone",
            notes="Tek düğüm test ortamı.",
            environment="test",
        )
        aapara_test_srv = await _get_or_create_server(
            session, customer, "aapara-test-node-1", "aapara-test-node-1.internal", "linux"
        )
        await _ensure_node(
            session, aapara, customer, aapara_test_group, aapara_test_srv, "aapara-test-node-1", 5432, "unknown"
        )

        await session.commit()
    print("Seed tamamlandı: X Bank (boa + aapara).")


if __name__ == "__main__":
    asyncio.run(seed())
