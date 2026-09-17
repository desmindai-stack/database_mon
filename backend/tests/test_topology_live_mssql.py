"""SQL Server topolojisi — gerçek sunucu, kısıtlı login'ler (Faz 31 Commit 6). Kurulum: scripts/live_mssql.py.

- Tek sunucu (Always On kapalı), VIEW SERVER STATE'li login, sihirbazın standalone yolu → "tek sunucu",
  cluster kuralı ve alarmı YOK.
- Yetkisiz login: SQL Server toplaması oturum DMV'sinde bile "permission denied" ile duruyor (ölçüldü);
  zamanlayıcının gerçek hata sınırı (`scheduler.collect_one_instance`) hatayı kaydediyor ve topoloji
  "ölçülemedi" + GRANT VIEW SERVER STATE dönüyor — hata yığını ya da alarm değil.
- Always On açık, ikinci replikası kopuk AG, VIEW SERVER STATE'li login → "cluster, bozuk", alarm VAR.
  Katalog görünümü bu login'e AG'yi göstermiyor; tespit DMV'den (ölçüldü).
- Aynı AG sunucusu, yetkisiz login → "ölçülemedi" + GRANT VIEW SERVER STATE, alarm YOK.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import AlertEvent, AlertRule, Instance
from app.collectors import scheduler
from app.services.credentials import encrypt_secret
from app.services.server_topology import DEGRADED_METRIC
from tests.live_mssql import LOGINS, MSSQL_SKIP_REASON, MSSQL_TARGETS, mssql_target, odbc_options

pytest.importorskip("aioodbc")
pytestmark = pytest.mark.skipif(set(MSSQL_TARGETS) != {"standalone", "ag"}, reason=MSSQL_SKIP_REASON)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


async def _wizard(kind: str, login: str) -> int:
    from tests.auth_helper import authed_client

    t = mssql_target(kind, login)
    await init_db()
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": f"mssql-topo-{uuid.uuid4().hex[:8]}"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        body = {"application_id": application["id"], "group_name": f"tek-{uuid.uuid4().hex[:6]}", "engine": "sqlserver",
                "topology": "standalone",
                "nodes": [{"server_name": f"srv-{uuid.uuid4().hex[:6]}", "host": t.host, "port": t.port, "database": "master",
                           "db_username": t.username, "db_password": LOGINS[login][1]}]}
        response = await client.post("/api/wizard/database-groups", json=body)
        assert response.status_code == 201, response.text
        nodes = (await client.get(f"/api/groups/{response.json()['id']}/nodes")).json()
    instance_id = nodes[0]["instance_id"]
    if odbc_options():
        async with SessionLocal() as session:
            instance = await session.get(Instance, instance_id)
            instance.options = {**(instance.options or {}), **odbc_options()}
            await session.commit()
    return instance_id


async def _direct(kind: str, login: str) -> int:
    t = mssql_target(kind, login)
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(name=f"mssql-topo-{uuid.uuid4().hex[:8]}", engine="sqlserver", host=t.host, port=t.port,
                            database="master", username=t.username, password=encrypt_secret(t.password),
                            options=odbc_options() or None, enabled=True)
        session.add(instance)
        await session.commit()
        return instance.id


async def _collect_and_read(instance_id: int) -> dict:
    from tests.auth_helper import authed_client

    await scheduler.collect_one_instance(instance_id)
    async with await authed_client() as client:
        body = (await client.get(f"/api/instances/{instance_id}/cluster-health")).json()
    async with SessionLocal() as session:
        events = (await session.execute(
            select(AlertRule.metric).join(AlertEvent, AlertEvent.rule_id == AlertRule.id)
            .where(AlertEvent.instance_id == instance_id, AlertEvent.resolved_at.is_(None))
        )).scalars().all()
        rules = (await session.execute(select(AlertRule.metric).where(AlertRule.instance_id == instance_id))).scalars().all()
    return {"topology": body["topology"], "services": [(s["service"], s["status"]) for s in body["services"]],
            "events": sorted(events), "rules": sorted(rules)}


async def test_standalone_sql_server_has_no_cluster_alarm():
    state = await _collect_and_read(await _wizard("standalone", "ro"))
    log("SQL Server tek sunucu, dbace_ro", state)
    assert state["topology"]["kind"] == "standalone"
    assert state["events"] == [] and state["rules"] == []
    assert state["services"] == [("sqlserver", "up")]


@pytest.mark.parametrize("kind", ["standalone", "ag"])
async def test_login_without_view_server_state_is_unmeasured_with_the_grant(kind):
    state = await _collect_and_read(await _wizard(kind, "noperm") if kind == "standalone" else await _direct(kind, "noperm"))
    log(f"SQL Server {kind}, dbace_noperm", state)
    assert state["topology"]["kind"] == "unmeasured"
    assert "permission" in state["topology"]["reason"].lower() or "yetki" in state["topology"]["reason"].lower()
    assert "VIEW SERVER STATE" in state["topology"]["required_grant"]
    assert state["events"] == [] and state["rules"] == []


async def test_availability_group_with_disconnected_replica_raises_the_alarm():
    state = await _collect_and_read(await _direct("ag", "ro"))
    log("SQL Server AG, kopuk replika, dbace_ro", state)
    assert (state["topology"]["kind"], state["topology"]["state"], state["topology"]["role"]) == ("cluster", "degraded", "primary")
    assert any(m["state"] == "DISCONNECTED" for m in state["topology"]["members"])
    assert state["events"] == [DEGRADED_METRIC]

