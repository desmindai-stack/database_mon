"""İzleme rolü = uygulama rolü tespiti — gerçek sunucuda, gerçek çağrı yoluyla (Faz 31 Commit 5).

Yol: toplayıcı (`collection.collect_instance`, pg_monitor rolü) pg_stat_activity'ye bakıyor →
`instances.monitoring_role_*` → API (GET /api/queries/{id}) `monitoring_role`.

- Hiç toplanmamış veritabanı: "unmeasured" — "ayrı" DEĞİL.
- AYRI rol: uygulama başka rolde çalışıyor, dbace'in kendi ek bağlantıları da açık → "separate".
- AYNI rol: uygulama dbace'in rolüyle bağlanıp imzasız sorgu çalıştırıyor → "shared", mesaj
  "ölçülemedi: izleme rolü uygulamayla paylaşılıyor", CREATE ROLE + GRANT pg_monitor bloğu.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.collectors.query_marker import connect_marked
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module
from app.services.credentials import encrypt_secret
from tests.live_pg import LIVE_DSNS, ROLE_PASSWORD, SKIP_REASON, dsn_id, prepare_live_database, target_for

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(params=LIVE_DSNS, ids=dsn_id)
def dsn(request):
    return request.param


@pytest.fixture
async def admin(dsn):
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    await prepare_live_database(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def _instance(dsn: str) -> int:
    t = target_for(dsn, "monitor")
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"role-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
            database=t.database, username=t.username, password=encrypt_secret(ROLE_PASSWORD), enabled=True,
        )
        session.add(instance)
        await session.commit()
        return instance.id


async def _collect(instance_id: int) -> None:
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()


async def _connect_as(dsn: str, role: str, application_name: str):
    t = target_for(dsn, role)
    return await asyncpg.connect(host=t.host, port=t.port, database=t.database, user=t.username,
                                 password=ROLE_PASSWORD, statement_cache_size=0,
                                 server_settings={"application_name": application_name})


async def _status(instance_id: int) -> dict:
    from tests.auth_helper import authed_client

    async with await authed_client() as client:
        response = await client.get(f"/api/queries/{instance_id}")
    assert response.status_code == 200, response.text
    return response.json()["monitoring_role"]


async def test_never_collected_is_unmeasured_not_separate(admin, dsn):
    status = await _status(await _instance(dsn))
    log("toplanmamış", status["status"])
    assert status["status"] == "unmeasured" and status["setup_command"] is None


async def test_separate_role_with_dbaces_own_extra_connections_is_separate(admin, dsn):
    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    instance_id = await _instance(dsn)
    app = await _connect_as(dsn, "app", "banka-uygulamasi")
    t = target_for(dsn, "monitor")
    # dbace'in KENDİ ek bağlantısı (imzalı, application_name=dbace) — paylaşım sayılmamalı.
    own = await connect_marked(host=t.host, port=t.port, database=t.database, user=t.username,
                               password=ROLE_PASSWORD, statement_cache_size=0)
    try:
        await app.fetchval("SELECT count(*) FROM orders WHERE status = 'paid'")
        await own.fetchval("SELECT 1")
        await _collect(instance_id)
        status = await _status(instance_id)
    finally:
        await app.close()
        await own.close()
    log(f"PG {version} ayrı rol", status)
    assert status["status"] == "separate", status
    assert status["applications"] == [] and status["setup_command"] is None


async def test_application_using_dbaces_role_is_shared_and_unmeasurable(admin, dsn):
    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    instance_id = await _instance(dsn)
    app = await _connect_as(dsn, "monitor", "banka-uygulamasi")
    try:
        await app.fetchval("SELECT count(*) FROM pg_class WHERE relpages > 10")
        await _collect(instance_id)
        status = await _status(instance_id)
    finally:
        await app.close()
    log(f"PG {version} aynı rol", status)
    assert status["status"] == "shared"
    assert status["message"].startswith("Ölçülemedi: izleme rolü uygulamayla paylaşılıyor")
    assert status["applications"] == ["banka-uygulamasi"]
    assert "CREATE ROLE dbace_monitor LOGIN" in status["setup_command"]
    assert "GRANT pg_monitor TO dbace_monitor;" in status["setup_command"]

    # Uygulama ayrıldıktan sonraki döngü kanıtı SİLMİYOR: gece çalışan iş gün içinde bağlı değildir.
    await asyncio.sleep(0.2)
    await _collect(instance_id)
    assert (await _status(instance_id))["status"] == "shared"
