"""Bankadaki SQL Server izleme login'iyle uçtan uca (Faz 31 Commit 8).

Login PAKETİN KENDİ kurulum SQL'iyle kuruluyor (deploy/onprem/sql/sqlserver-monitor-login.sql):
VIEW SERVER STATE + VIEW DATABASE STATE; sysadmin/db_owner yok. Gerçek bir deadlock üretiliyor ve
zamanlayıcının gerçek turları (toplama + olay yakalama) koşuyor; her özellik ya ölçüyor ya da
"ölçülemedi + gerekçe + gereken yetki" dönüyor. Yetkisiz login'de deadlock geçmişi "deadlock olmadı"
DEMİYOR.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from app.collectors import scheduler
from app.collectors.sqlserver_mongodb import build_odbc_connection_string
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import plan_capture
from tests.live_mssql import (
    APP_DATABASE,
    LOGINS,
    MONITOR_LOGIN,
    MONITOR_PASSWORD,
    MSSQL_SKIP_REASON,
    MSSQL_TARGETS,
    NO_USER_DATABASE,
    SA_PASSWORD,
    odbc_options,
    prepare_monitor_login,
    standalone_target,
)

pyodbc = pytest.importorskip("pyodbc")
pytest.importorskip("aioodbc")
pytestmark = pytest.mark.skipif("standalone" not in MSSQL_TARGETS, reason=MSSQL_SKIP_REASON)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


_target = standalone_target
_prepare = prepare_monitor_login


def _deadlock() -> str:
    """İki oturum ters sırayla kilitliyor — SQL Server birini kurban seçiyor (1205)."""
    a, b = (pyodbc.connect(build_odbc_connection_string(_target("sa", SA_PASSWORD, APP_DATABASE)), autocommit=False)
            for _ in range(2))
    errors: list[str] = []
    try:
        a.cursor().execute("UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = 1")
        b.cursor().execute("UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = 2")

        def second(conn, row_id):
            try:
                conn.cursor().execute(f"UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = {row_id}")
            except pyodbc.Error as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=second, args=(a, 2)), threading.Thread(target=second, args=(b, 1))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    finally:
        for conn in (a, b):
            try:
                conn.rollback()
            finally:
                conn.close()
    assert any("1205" in e or "deadlock" in e.lower() for e in errors), errors
    return errors[0]


async def _wizard(user: str, password: str, database: str = APP_DATABASE) -> int:
    from tests.auth_helper import authed_client

    t = _target(user, password, database)
    tag = uuid.uuid4().hex[:6]
    await init_db()
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": f"mssql-kısıtlı-{tag}"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        response = await client.post("/api/wizard/database-groups", json={
            "application_id": application["id"], "group_name": f"ms-{tag}", "engine": "sqlserver", "topology": "standalone",
            "nodes": [{"server_name": f"ms-{tag}", "host": t.host, "port": t.port, "database": database,
                       "db_username": user, "db_password": password}],
        })
        assert response.status_code == 201, response.text
        instance_id = (await client.get(f"/api/groups/{response.json()['id']}/nodes")).json()[0]["instance_id"]
    if odbc_options():
        async with SessionLocal() as session:
            instance = await session.get(Instance, instance_id)
            instance.options = {**(instance.options or {}), **odbc_options()}
            await session.commit()
    return instance_id


async def _read(instance_id: int) -> dict:
    from tests.auth_helper import authed_client

    async with await authed_client() as client:
        return {
            "instance": (await client.get(f"/api/instances/{instance_id}")).json(),
            "topology": (await client.get(f"/api/instances/{instance_id}/cluster-health")).json()["topology"],
            "queries": (await client.get(f"/api/queries/{instance_id}")).json(),
            "prerequisites": (await client.get(f"/api/instances/{instance_id}/prerequisites")).json(),
            "blocking": (await client.get(f"/api/instances/{instance_id}/blocking")).json(),
            "history": (await client.get(f"/api/instances/{instance_id}/blocking-history")).json(),
        }


async def test_package_login_measures_everything_including_a_real_deadlock():
    await asyncio.to_thread(_prepare)
    conn = pyodbc.connect(build_odbc_connection_string(_target(MONITOR_LOGIN, MONITOR_PASSWORD, APP_DATABASE)))
    cur = conn.cursor()
    grants = {
        "sysadmin": cur.execute("SELECT IS_SRVROLEMEMBER('sysadmin')").fetchval(),
        "db_owner": cur.execute("SELECT IS_MEMBER('db_owner')").fetchval(),
        "CONTROL SERVER": cur.execute("SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'CONTROL SERVER')").fetchval(),
        "VIEW SERVER STATE": cur.execute("SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE')").fetchval(),
        "VIEW DATABASE STATE": cur.execute("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE')").fetchval(),
        "INSERT deadlock_probe": cur.execute("SELECT HAS_PERMS_BY_NAME('dbo.deadlock_probe', 'OBJECT', 'INSERT')").fetchval(),
    }
    conn.close()
    log("dbace_monitor yetkileri", grants)
    assert grants == {"sysadmin": 0, "db_owner": 0, "CONTROL SERVER": 0, "VIEW SERVER STATE": 1,
                      "VIEW DATABASE STATE": 1, "INSERT deadlock_probe": 0}

    victim = await asyncio.to_thread(_deadlock)
    log("üretilen deadlock", victim[:120])
    instance_id = await _wizard(MONITOR_LOGIN, MONITOR_PASSWORD)
    await scheduler.collect_one_instance(instance_id)
    ticks = await plan_capture.capture_plans_tick()
    body = await _read(instance_id)

    unexplained = [c["key"] for c in body["prerequisites"]["checks"] if c["status"] != "ok" and not (c["detail"] or c["fix"])]
    log("toplama/topoloji", (body["instance"].get("last_collect_error"), body["topology"]["kind"], body["topology"]["reason"]))
    log("ön koşullar", [(c["key"], c["status"]) for c in body["prerequisites"]["checks"]])
    log("yavaş sorgu", (len(body["queries"]["items"]), body["queries"].get("unavailable_reason")))
    log("bloklama", body["blocking"].get("unavailable_reason"))
    log("olay turu", ticks)
    log("deadlock geçmişi", ([(d["source"], (d["victim_query"] or "")[:50]) for d in body["history"]["deadlocks"]],
                              body["history"]["deadlock_detail_reason"]))

    assert not body["instance"].get("last_collect_error")
    assert body["topology"]["kind"] == "standalone" and body["topology"]["required_grant"] is None
    assert body["queries"]["items"], "VIEW SERVER STATE ile sorgu istatistikleri okunmalı"
    assert not unexplained, unexplained
    assert body["blocking"].get("unavailable_reason") is None
    assert body["history"]["deadlock_detail_reason"] is None
    assert any("deadlock_probe" in (d["victim_query"] or "") + (d["winner_query"] or "") for d in body["history"]["deadlocks"])


@pytest.mark.parametrize(("database", "needed"), [
    ("master", "GRANT VIEW SERVER STATE TO"),
    (NO_USER_DATABASE, f"USE [{NO_USER_DATABASE}]; CREATE USER"),
])
async def test_login_without_grants_says_deadlocks_not_measured_with_the_grant_it_lacks(database, needed):
    """Negatif kontrol: yetkisiz login'de boş deadlock listesi "olmadı" diye gösterilmiyor; gereken yetki
    GERÇEK hatadan (izleme yetkisi yok / veritabanında kullanıcı yok)."""
    await asyncio.to_thread(_prepare)
    user, password = LOGINS["noperm"]
    instance_id = await _wizard(user, password, database)
    await scheduler.collect_one_instance(instance_id)
    await plan_capture.capture_plans_tick()
    history = (await _read(instance_id))["history"]
    log(f"yetkisiz login, {database}", (history["deadlocks"], history["deadlock_detail_reason"]))
    assert history["deadlocks"] == []
    assert history["deadlock_detail_reason"].startswith("Deadlock geçmişi ölçülemedi")
    assert needed in history["deadlock_detail_reason"]
    assert needed in history["unavailable_reason"]
