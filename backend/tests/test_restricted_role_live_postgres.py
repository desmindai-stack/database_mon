"""Bankadaki izleme kullanıcısıyla uçtan uca (Faz 31 Commit 8).

Rol PAKETİN KENDİ kurulum SQL'iyle kuruluyor (deploy/onprem/sql/postgresql-monitor-role.sql):
pg_monitor + şemada SELECT. Veritabanında TEMP/CREATE PUBLIC'ten alınmış; superuser,
pg_read_server_files, CREATE EXTENSION yok. Her özellik ya ÖLÇÜYOR ya da "ölçülemedi + gerekçe +
gereken yetki" dönüyor — boş ekran ya da hata yığını değil.

Yol gerçek: sihirbaz → toplama turu → HTTP uçları → izlenen PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module
from tests.live_pg import (
    LIVE_DSNS,
    RESTRICTED_DATABASE,
    RESTRICTED_ROLE,
    ROLE_PASSWORD,
    SKIP_REASON,
    UNGRANTED_TABLE,
    dsn_id,
    prepare_restricted_database,
    restricted_target,
    with_database,
)

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(params=LIVE_DSNS, ids=dsn_id)
def dsn(request):
    return request.param


@pytest.fixture
async def admin(dsn):
    await prepare_restricted_database(dsn)
    conn = await asyncpg.connect(with_database(dsn, RESTRICTED_DATABASE), statement_cache_size=0)
    try:
        yield conn
    finally:
        await conn.close()


async def _monitor(dsn: str):
    t = restricted_target(dsn)
    return await asyncpg.connect(host=t.host, port=t.port, database=t.database, user=t.username,
                                 password=t.password, statement_cache_size=0)


async def test_package_role_is_read_only_on_the_real_server(admin, dsn):
    """Paket rolünün GERÇEK yetkileri: pg_monitor üyesi, başka hiçbir şey."""
    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    conn = await _monitor(dsn)
    try:
        facts = dict(await conn.fetchrow(
            """
            SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
                   pg_has_role(current_user, 'pg_monitor', 'USAGE') AS pg_monitor,
                   pg_has_role(current_user, 'pg_read_server_files', 'USAGE') AS read_server_files,
                   pg_has_role(current_user, 'pg_write_server_files', 'USAGE') AS write_server_files,
                   pg_has_role(current_user, 'pg_execute_server_program', 'USAGE') AS execute_program,
                   has_database_privilege(current_database(), 'TEMP') AS temp,
                   has_database_privilege(current_database(), 'CREATE') AS create_db,
                   has_schema_privilege('public', 'CREATE') AS create_schema,
                   has_table_privilege('public.orders', 'INSERT,UPDATE,DELETE,TRUNCATE') AS write_orders,
                   has_table_privilege('public.orders', 'SELECT') AS select_orders,
                   has_table_privilege('public.secret_ledger', 'SELECT') AS select_secret
            FROM pg_roles r WHERE r.rolname = current_user
            """
        ))
        denied = {}
        for label, sql in {
            "CREATE TEMP TABLE": "CREATE TEMP TABLE dbace_probe (id int)",
            "CREATE TABLE": "CREATE TABLE public.dbace_probe (id int)",
            "CREATE EXTENSION": "CREATE EXTENSION IF NOT EXISTS pg_buffercache",
            "pg_read_file": "SELECT pg_read_file('postgresql.conf')",
            "INSERT": "INSERT INTO marker_probe VALUES (100000, 'x')",
        }.items():
            try:
                await conn.execute(sql)
                denied[label] = "İZİN VERİLDİ"
            except asyncpg.PostgresError as exc:
                denied[label] = f"{type(exc).__name__}: {str(exc)[:70]}"
    finally:
        await conn.close()
    log(f"PG {version} {RESTRICTED_ROLE} yetkileri", facts)
    log(f"PG {version} reddedilenler", denied)
    assert facts["pg_monitor"] is True and facts["select_orders"] is True
    assert not any(v for k, v in facts.items() if k not in ("pg_monitor", "select_orders")), facts
    assert all(v.startswith("InsufficientPrivilege") for v in denied.values()), denied


async def _deadlock(dsn: str) -> None:
    """Gerçek bir deadlock — sayaç (pg_stat_database.deadlocks) artsın."""
    url = with_database(dsn, RESTRICTED_DATABASE)
    a = await asyncpg.connect(url, statement_cache_size=0)
    b = await asyncpg.connect(url, statement_cache_size=0)
    try:
        await a.execute("SET deadlock_timeout = '100ms'")
        await b.execute("SET deadlock_timeout = '100ms'")
        ta, tb = a.transaction(), b.transaction()
        await ta.start()
        await tb.start()
        await a.execute("UPDATE deadlock_probe SET v = v + 1 WHERE id = 1")
        await b.execute("UPDATE deadlock_probe SET v = v + 1 WHERE id = 2")
        results = await asyncio.gather(
            a.execute("UPDATE deadlock_probe SET v = v + 1 WHERE id = 2"),
            b.execute("UPDATE deadlock_probe SET v = v + 1 WHERE id = 1"),
            return_exceptions=True,
        )
        assert any(isinstance(r, asyncpg.exceptions.DeadlockDetectedError) for r in results), results
        for tx in (ta, tb):
            try:
                await tx.rollback()
            except Exception:
                pass
    finally:
        await a.close()
        await b.close()


async def _collect(instance_id: int) -> None:
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()
    await asyncio.sleep(1.1)


async def test_every_feature_measures_or_explains_with_the_package_role(admin, dsn):
    from tests.auth_helper import authed_client

    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    await admin.execute("SELECT pg_stat_statements_reset()")
    t = restricted_target(dsn)
    tag = f"rr{uuid.uuid4().hex[:6]}"
    await init_db()
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": f"kısıtlı-{tag}"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        group = (await client.post("/api/wizard/database-groups", json={
            "application_id": application["id"], "group_name": f"g-{tag}", "engine": "postgresql", "topology": "standalone",
            "nodes": [{"server_name": f"s-{tag}", "host": t.host, "port": t.port, "database": t.database,
                       "db_username": t.username, "db_password": ROLE_PASSWORD}],
        })).json()
        instance_id = (await client.get(f"/api/groups/{group['id']}/nodes")).json()[0]["instance_id"]

        await _collect(instance_id)
        for i in range(3):
            await admin.fetchval(f"SELECT count(*) AS {tag}_{i} FROM orders WHERE status = $1 AND (SELECT pg_sleep(0.06)) IS NOT NULL", "paid")
        await _deadlock(dsn)
        await _collect(instance_id)

        instance = (await client.get(f"/api/instances/{instance_id}")).json()
        topology = (await client.get(f"/api/instances/{instance_id}/cluster-health")).json()["topology"]
        queries = (await client.get(f"/api/queries/{instance_id}", params={"sort": "mean", "limit": 20})).json()
        prerequisites = (await client.get(f"/api/instances/{instance_id}/prerequisites")).json()
        advice = (await client.post(f"/api/queries/{instance_id}/advice", json={
            "query": "SELECT id FROM adv_users WHERE lower(email) = $1", "calls": 100000})).json()
        explain_ok = await client.post(f"/api/queries/{instance_id}/explain", json={
            "query": "SELECT count(*) FROM orders WHERE status = 'paid'", "analyze": True})
        explain_denied = await client.post(f"/api/queries/{instance_id}/explain", json={
            "query": f"SELECT sum(amount) FROM {UNGRANTED_TABLE} WHERE id = 3", "analyze": True})
        plans = (await client.get(f"/api/queries/{instance_id}/captured-plans")).json()
        history = (await client.get(f"/api/instances/{instance_id}/blocking-history")).json()
        schema_health = await client.get(f"/api/instances/{instance_id}/schema-health")
        insights = await client.get(f"/api/instances/{instance_id}/insights")

    expression = [a for a in advice["advice"] if a["index_kind"] == "expression"]
    unexplained = [c["key"] for c in prerequisites["checks"] if c["status"] != "ok" and not (c["detail"] or c["fix"])]
    log(f"PG {version} toplama", {"hata": instance.get("last_collect_error"), "topoloji": (topology["kind"], topology["reason"][:60])})
    log(f"PG {version} yavaş sorgu", [(q["query"][:40], round(q["mean_time_ms"])) for q in queries["items"][:3]])
    log(f"PG {version} ön koşullar", [(c["key"], c["status"]) for c in prerequisites["checks"]])
    log(f"PG {version} ifade index'i", [(a["index_ddl"], a["verified"], a["verification_note"]) for a in expression])
    log(f"PG {version} EXPLAIN ANALYZE yetkili", (explain_ok.status_code, explain_ok.json().get("analyzed")))
    log(f"PG {version} EXPLAIN ANALYZE yetkisiz", (explain_denied.status_code, explain_denied.json()["detail"]))
    log(f"PG {version} plan yakalama", (plans["unavailable_kind"], plans["unavailable_reason"]))
    log(f"PG {version} deadlock", (history["deadlock_counter"], history["deadlock_detail_reason"]))

    assert not instance.get("last_collect_error"), instance.get("last_collect_error")
    # Önemli olan ÖLÇÜLEBİLMESİ: pg_monitor pg_stat_replication/pg_is_in_recovery okuyabiliyor, gereken yetki yok.
    # (Replika ayakta ise "cluster/healthy", değilse "tek sunucu" — ikisi de ölçülmüş sonuç; "ölçülemedi" değil.)
    assert topology["kind"] in ("standalone", "cluster") and topology["required_grant"] is None, topology
    assert topology["reason"] and topology["checked_at"], topology
    if topology["kind"] == "cluster":
        assert topology["state"] == "healthy", topology
    assert any(q["query"] != "<insufficient privilege>" and tag in q["query"] for q in queries["items"]), \
        "pg_read_all_stats ile başka rolün sorgu metni okunmalı"
    assert not unexplained, f"gerekçesiz ön koşul sonucu: {unexplained}"

    # hypopg DBA tarafından kurulmuş: TEMP/CREATE olmadan ifade index'i DOĞRULANIYOR.
    assert len(expression) == 1 and expression[0]["verified"] is True, expression
    assert "TEMP" not in str(advice.get("required_grants"))

    assert explain_ok.status_code == 200 and explain_ok.json()["analyzed"] is True
    detail = explain_denied.json()["detail"]
    assert explain_denied.status_code == 400
    assert detail.startswith("Ölçülemedi") and f"GRANT SELECT ON {UNGRANTED_TABLE} TO" in detail

    assert plans["unavailable_kind"] == "no_agent"
    assert plans["unavailable_reason"].startswith("Ölçülemedi") and "pg_read_server_files" in plans["unavailable_reason"]

    assert history["deadlocks"] == []
    assert history["deadlock_counter"] is not None and history["deadlock_counter"] >= 1, history
    reason = history["deadlock_detail_reason"]
    assert reason and "ölçülemedi" in reason and "host-agent" in reason and "pg_read_server_files" in reason

    assert schema_health.status_code == 200, schema_health.text[:300]
    assert insights.status_code == 200, insights.text[:300]
