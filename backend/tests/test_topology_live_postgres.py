"""Tek sunucuda cluster alarmı YOK, replika koparılınca VAR, yetki eksikse "ölçülemedi" — gerçek sunucu
(Faz 31 Commit 6).

Kurulum (scripts/live_pg.py): her sürümde birincil + streaming replika. Replika SQL'le koparılıyor
(`ALTER SYSTEM SET primary_conninfo = ''`) ve her testten sonra geri bağlanıyor — docker CLI gerekmiyor.

Yol: veritabanı sihirbazın API'siyle (standalone grup — hatanın üretildiği yol) ya da düğüm/rol
ayrımı gereken yerde doğrudan ekleniyor → GERÇEK toplama turu (`collection.collect_instance`) →
GET /api/instances/{id}/cluster-health `topology` → alarm olayları dbace veritabanından.

"Tek sunucu" testi bu makinede kurulu GERÇEK host-agent'ı (agents/host-agent, alt süreç) bağlıyor:
hatanın tetikleyicisi agent'ın sunucuda olmayan patroni/etcd birimleri için döndürdüğü durumdu.

Bütün topoloji okumaları KISITLI rollerle: pg_monitor'lu `dbace_it_monitor`, hiçbir izleme yetkisi
olmayan `dbace_it_app`.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import AlertEvent, AlertRule, Instance
from app.services import collection as collection_module
from app.services.credentials import encrypt_secret
from app.services.server_topology import DEGRADED_METRIC
from tests.live_pg import LIVE_DSNS, ROLE_PASSWORD, SKIP_REASON, dsn_id, prepare_live_database, replica_for, target_for

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

AGENT_DIR = Path(__file__).resolve().parents[2] / "agents" / "host-agent"
AGENT_TOKEN = "topology-it"


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


@pytest.fixture
async def replica(dsn):
    """Replikanın süper kullanıcı bağlantısı — yalnızca TEST ALTYAPISI koparıp bağlamak için; dbace
    bu bağlantıyı kullanmıyor. Test sonunda bağlantı her durumda geri yazılıyor."""
    conn = await asyncpg.connect(replica_for(dsn), statement_cache_size=0)
    original = await conn.fetchval("SHOW primary_conninfo")
    assert original, "replika primary_conninfo boş — `python scripts/live_pg.py up` geri bağlar"
    try:
        yield conn
    finally:
        await conn.execute(await conn.fetchval("SELECT format('ALTER SYSTEM SET primary_conninfo = %L', $1::text)", original))
        await conn.execute("SELECT pg_reload_conf()")
        await _wait(lambda: conn.fetchval("SELECT status FROM pg_stat_wal_receiver"), "streaming")
        await conn.close()


@pytest.fixture(scope="module")
def host_agent():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=AGENT_DIR, env={**os.environ, "AGENT_TOKEN": AGENT_TOKEN},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/v1/services", headers={"X-Agent-Token": AGENT_TOKEN}, timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        services = httpx.get(f"{url}/v1/services", headers={"X-Agent-Token": AGENT_TOKEN}, timeout=10).json()["services"]
        assert all(v["active"] != "active" for v in services.values()), services
        yield {"agent_url": url, "agent_token": AGENT_TOKEN, "services": services}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def _wait(fetch, expected, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = await fetch()
        if value == expected:
            return value
        await asyncio.sleep(0.5)
    raise AssertionError(f"{timeout} sn içinde beklenen {expected!r} olmadı (son: {value!r})")


async def _break(replica_conn, primary_admin):
    await replica_conn.execute("ALTER SYSTEM SET primary_conninfo = ''")
    await replica_conn.execute("SELECT pg_reload_conf()")
    await _wait(lambda: primary_admin.fetchval("SELECT count(*) FROM pg_stat_replication"), 0)


def _direct_instance(dsn: str, role: str, **extra) -> Instance:
    t = target_for(dsn, role)
    return Instance(name=f"topo-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
                    database=t.database, username=t.username, password=encrypt_secret(ROLE_PASSWORD), enabled=True, **extra)


async def _add(instance: Instance) -> int:
    await init_db()
    async with SessionLocal() as session:
        session.add(instance)
        await session.commit()
        return instance.id


async def _wizard_standalone(dsn: str, role: str, agent: dict) -> int:
    from tests.auth_helper import authed_client

    t = target_for(dsn, role)
    await init_db()
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": f"topo-{uuid.uuid4().hex[:8]}"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        body = {
            "application_id": application["id"], "group_name": f"tek-{uuid.uuid4().hex[:6]}", "engine": "postgresql",
            "topology": "standalone",
            "nodes": [{"server_name": f"srv-{uuid.uuid4().hex[:6]}", "host": t.host, "port": t.port, "database": t.database,
                       "db_username": t.username, "db_password": ROLE_PASSWORD, "agent_url": agent["agent_url"],
                       "agent_token": agent["agent_token"]}],
        }
        response = await client.post("/api/wizard/database-groups", json=body)
        assert response.status_code == 201, response.text
        nodes = (await client.get(f"/api/groups/{response.json()['id']}/nodes")).json()
    instance_id = nodes[0]["instance_id"]
    async with SessionLocal() as session:
        instance = await session.get(Instance, instance_id)
        # Plan yakalama için tek sunucuya kurulan agent (hatanın tetikleyicisi) — instance düzeyinde.
        instance.options = {**(instance.options or {}), "agent_url": agent["agent_url"], "agent_token": agent["agent_token"]}
        await session.commit()
    return instance_id


async def _collect(instance_id: int) -> None:
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()


async def _state(instance_id: int) -> dict:
    from tests.auth_helper import authed_client

    async with await authed_client() as client:
        response = await client.get(f"/api/instances/{instance_id}/cluster-health")
    assert response.status_code == 200, response.text
    body = response.json()
    async with SessionLocal() as session:
        events = (await session.execute(
            select(AlertRule.metric).join(AlertEvent, AlertEvent.rule_id == AlertRule.id)
            .where(AlertEvent.instance_id == instance_id, AlertEvent.resolved_at.is_(None))
        )).scalars().all()
        rules = (await session.execute(select(AlertRule.metric).where(AlertRule.instance_id == instance_id))).scalars().all()
    return {"topology": body["topology"], "overall": body["overall"], "totals": body["totals"],
            "services": [(s["service"], s["status"]) for s in body["services"]],
            "events": sorted(events), "rules": sorted(rules)}


async def _version(admin) -> str:
    return (await admin.fetchval("SHOW server_version")).split(" ")[0]


async def test_standalone_with_real_host_agent_has_no_cluster_alarm(admin, replica, dsn, host_agent):
    await _break(replica, admin)  # birincil artık replikasız: tek sunucu
    instance_id = await _wizard_standalone(dsn, "monitor", host_agent)
    await _collect(instance_id)
    await _collect(instance_id)
    state = await _state(instance_id)
    log(f"PG {await _version(admin)} tek sunucu + gerçek agent", state | {"agent": {k: v["active"] for k, v in host_agent["services"].items()}})
    assert state["topology"]["kind"] == "standalone", state
    assert state["events"] == [] and state["rules"] == []
    assert state["totals"]["down"] == 0 and state["overall"] == "healthy"
    assert [name for name, _ in state["services"]] == ["postgresql"]


async def test_breaking_the_real_replica_raises_the_alarm_on_primary_and_replica(admin, replica, dsn):
    version = await _version(admin)
    primary_id = await _add(_direct_instance(dsn, "monitor"))
    replica_id = await _add(_direct_instance(replica_for(dsn), "monitor"))
    for iid in (primary_id, replica_id):
        await _collect(iid)
    before = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} bağlı", {k: (v["topology"]["kind"], v["topology"]["state"], v["topology"]["role"], v["events"]) for k, v in before.items()})
    for view in before.values():
        assert (view["topology"]["kind"], view["topology"]["state"]) == ("cluster", "healthy")
        assert view["events"] == [] and DEGRADED_METRIC in view["rules"]

    await _break(replica, admin)
    await _wait(lambda: replica.fetchval("SELECT count(*) FROM pg_stat_wal_receiver"), 0)
    for iid in (primary_id, replica_id):
        await _collect(iid)
    after = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} koparıldı", {k: (v["topology"]["kind"], v["topology"]["state"], v["topology"]["reason"], v["events"]) for k, v in after.items()})
    for view in after.values():
        assert (view["topology"]["kind"], view["topology"]["state"]) == ("cluster", "degraded")
        assert view["events"] == [DEGRADED_METRIC]


async def test_missing_privilege_is_unmeasured_with_the_grant_and_no_alarm(admin, replica, dsn):
    version = await _version(admin)
    primary_id = await _add(_direct_instance(dsn, "app"))
    replica_id = await _add(_direct_instance(replica_for(dsn), "app"))
    for iid in (primary_id, replica_id):
        await _collect(iid)
    views = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} pg_monitor'suz rol", {k: (v["topology"]["kind"], v["topology"]["reason"], v["topology"]["required_grant"], v["events"]) for k, v in views.items()})
    for view in views.values():
        assert view["topology"]["kind"] == "unmeasured"
        assert "pg_monitor" in view["topology"]["required_grant"]
        assert view["events"] == [] and view["rules"] == []
