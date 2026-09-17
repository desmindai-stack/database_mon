"""İmza filtresinin TERS yönü: imzalı metin kimden geldi (Faz 31 Commit 4).

Ölçülen gerçekler (PG 15.19 / 16.15 / 17.11):
- dbace aynı şekildeki sorguyu uygulamadan ÖNCE çalıştırsa bile, roller FARKLIYSA
  pg_stat_statements iki AYRI satır tutuyor (userid anahtarın parçası) ve uygulama satırının
  metni imzasız.
- Asıl açık dbace'in kendi seçimindeydi: satırlar yalnızca queryid ile birleştiriliyordu, imzalı
  dbace satırı ile uygulama satırı tek gruba düşüyor ve uygulama yükü "sistem sorgusu" diye
  gizlenebiliyordu.
- Uygulama dbace ile AYNI rolü paylaşıyorsa çağrılar tek satırda birleşiyor; kaynakta ayrım yok.

Çalıştırma: bkz. tests/live_pg.py.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.collectors.query_marker import DBACE_QUERY_MARKER
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module
from app.services.credentials import encrypt_secret
from app.services.index_advice_watch import current_calls
from app.services.slow_query_selection import select_slow_queries
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


async def _as(dsn: str, role: str):
    t = target_for(dsn, role)
    return await asyncpg.connect(
        host=t.host, port=t.port, database=t.database, user=t.username, password=ROLE_PASSWORD, statement_cache_size=0
    )


async def _instance(dsn: str) -> Instance:
    t = target_for(dsn, "super")
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"origin-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
            database=t.database, username=t.username, password=encrypt_secret(t.password), enabled=True,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance


async def _collect(instance_id: int) -> None:
    """Tek toplama döngüsü. Testler iş yükünü İKİ döngü ARASINA koyuyor: fark, iki örnek
    arasındaki çağrıları sayar — sorgu ilk kez ikinci döngüde görülürse farkı 0'dır (doğru
    davranış; ilk yazılan test bunu atlayıp PG 15'te zamanlamaya takıldı)."""
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        instance = await session.get(Instance, instance_id)
        await collection_module.collect_instance(instance, session)
        await session.commit()
    # collected_at saniye çözünürlüğünde saklanıyor (SQLite sunucu varsayılanı). Aynı saniyedeki iki
    # döngü penceresi TEK döngü sayılır ve seçim fark yerine kümülatif gösterir (mode=snapshot).
    await asyncio.sleep(1.1)


async def _selection(instance_id: int, include_system: bool):
    async with SessionLocal() as session:
        return await select_slow_queries(
            session, instance_id, include_system=include_system, min_total_ms=0, min_calls=0, limit=1000
        )


async def _queryid(admin, tag: str) -> str:
    return await admin.fetchval(
        "SELECT queryid::text FROM pg_stat_statements WHERE query LIKE $1 AND query NOT LIKE '%pg_stat_statements%' LIMIT 1",
        f"%{tag}%",
    )


async def _version(admin) -> str:
    return (await admin.fetchval("SHOW server_version")).split(" ")[0]


async def test_dbace_first_then_application_in_another_role_keeps_application_load_visible(admin, dsn):
    tag = f"ord_{uuid.uuid4().hex[:6]}"
    shape = f"SELECT count(*) AS {tag} FROM orders WHERE (SELECT pg_sleep(0.03)) IS NOT NULL AND status = "
    instance = await _instance(dsn)
    await admin.execute("SELECT pg_stat_statements_reset()")
    dbace, app = await _as(dsn, "super"), await _as(dsn, "app")
    try:
        # dbace ÖNCE (imzalı), sonra uygulama — ikisi de taban döngüsünde görülüyor.
        await dbace.execute(f"{DBACE_QUERY_MARKER} {shape}'dbace-once'")
        await app.execute(f"{shape}'once'")
        await _collect(instance.id)
        for value in ("a", "b", "c", "d"):
            await app.execute(f"{shape}'{value}'")
            await dbace.execute(f"{DBACE_QUERY_MARKER} {shape}'dbace-{value}'")
        await _collect(instance.id)
    finally:
        await dbace.close()
        await app.close()

    queryid = await _queryid(admin, tag)
    hidden = await _selection(instance.id, include_system=False)
    shown = await _selection(instance.id, include_system=True)
    app_entries = [e for e in hidden.entries if e.queryid == queryid]
    dbace_entries = [e for e in shown.entries if e.queryid == queryid and e.key.endswith(":dbace")]
    log(
        await _version(admin),
        f"varsayılan listede bu queryid: {[(e.key, e.calls, e.query[:40]) for e in app_entries]} | "
        f"sistem dahil dbace satırı: {[(e.key, e.is_system) for e in dbace_entries]}",
    )
    assert hidden.mode == "delta"
    assert len(app_entries) == 1, "uygulama yükü görünmüyor ya da dbace satırıyla birleşmiş"
    assert app_entries[0].key == f"id:{queryid}", "uygulama satırı eski (derin bağlantı) anahtarını korumalı"
    assert DBACE_QUERY_MARKER not in app_entries[0].query and not app_entries[0].is_system
    assert app_entries[0].calls == 4, "fark hesabı dbace'in çağrılarıyla karışmamalı"
    assert len(dbace_entries) == 1 and dbace_entries[0].is_system


async def test_marked_text_from_a_non_dbace_role_is_shown_flagged_and_advisable(admin, dsn):
    """İmzayı kopyalayan bir araç/uygulama: metin imzalı, userid dbace değil → gizlenmez."""
    from tests.auth_helper import authed_client

    tag = f"ord_{uuid.uuid4().hex[:6]}"
    instance = await _instance(dsn)
    await admin.execute("SELECT pg_stat_statements_reset()")
    app = await _as(dsn, "app")
    try:
        await app.execute(f"{DBACE_QUERY_MARKER} SELECT count(*) AS {tag} FROM orders WHERE (SELECT pg_sleep(0.03)) IS NOT NULL AND status = 'once'")
        await _collect(instance.id)
        for value in ("x", "y", "z", "w", "v", "u"):
            await app.execute(f"{DBACE_QUERY_MARKER} SELECT count(*) AS {tag} FROM orders WHERE (SELECT pg_sleep(0.03)) IS NOT NULL AND status = '{value}'")
    finally:
        await app.close()
    await _collect(instance.id)

    queryid = await _queryid(admin, tag)
    entries = [e for e in (await _selection(instance.id, include_system=False)).entries if e.queryid == queryid]
    async with await authed_client() as client:
        listing = (await client.get(f"/api/queries/{instance.id}", params={"include_system": "false", "limit": 100})).json()
        item = next(i for i in listing["items"] if i["queryid"] == queryid)
        advice = (await client.post(
            f"/api/queries/{instance.id}/advice", json={"query": item["query"], "queryid": queryid, "calls": 6}
        )).json()
    log(
        await _version(admin),
        f"liste: marker_conflict={item['marker_conflict']} not={item['marker_note'][:60]!r} | index önerisi durumu={advice['status']}",
    )
    assert len(entries) == 1 and entries[0].marker_conflict and not entries[0].is_system
    assert item["marker_conflict"] is True and item["marker_note"]
    assert advice["status"] != "system", "işaretli uygulama yükü için öneri 'sistem sorgusu' diye reddedilmemeli"


async def test_application_sharing_dbaces_role_is_indistinguishable_documented_limit(admin, dsn):
    """Bilinen sınır: aynı rolde çağrılar kaynakta tek satır. Test sınırı SABİTLİYOR — değişirse
    (ör. PostgreSQL ayrım sunarsa) kırılır ve SORULAR.md güncellenir."""
    tag = f"ord_{uuid.uuid4().hex[:6]}"
    await admin.execute("SELECT pg_stat_statements_reset()")
    shared = await _as(dsn, "super")
    try:
        await shared.execute(f"{DBACE_QUERY_MARKER} SELECT count(*) AS {tag} FROM orders WHERE (SELECT pg_sleep(0.03)) IS NOT NULL AND status = 'dbace'")
        for value in ("app1", "app2"):
            await shared.execute(f"SELECT count(*) AS {tag} FROM orders WHERE (SELECT pg_sleep(0.03)) IS NOT NULL AND status = '{value}'")
    finally:
        await shared.close()
    rows = await admin.fetch(
        "SELECT calls, query FROM pg_stat_statements WHERE query LIKE $1 AND query NOT LIKE '%pg_stat_statements%'",
        f"%{tag}%",
    )
    log(await _version(admin), [(r["calls"], r["query"][:60]) for r in rows])
    assert len(rows) == 1 and rows[0]["calls"] == 3 and rows[0]["query"].startswith(DBACE_QUERY_MARKER)


async def test_nested_executions_are_a_separate_series_and_not_counted_as_top_level(admin, dsn):
    # İşlev gövdesindeki sorguyla AYNI şekil (tests/live_pg.py::adv_nested_count).
    tag_query = "SELECT count(*) FROM orders WHERE status = 'paid' AND (SELECT pg_sleep(0.03)) IS NOT NULL"
    instance = await _instance(dsn)
    await admin.execute("ALTER ROLE dbace_it_app SET pg_stat_statements.track = 'all'")
    try:
        await admin.execute("SELECT pg_stat_statements_reset()")
        app = await _as(dsn, "app")
        try:
            await app.execute(tag_query)
            await app.execute("SELECT adv_nested_count()")
            await _collect(instance.id)
            for _ in range(3):
                await app.execute("SELECT adv_nested_count()")
            await app.execute(tag_query)
            await _collect(instance.id)
        finally:
            await app.close()
    finally:
        await admin.execute("ALTER ROLE dbace_it_app RESET pg_stat_statements.track")

    # İç içe satırın METNİ farklı saklanıyor (PL/pgSQL `INTO`'yu çıkarıyor) ama queryid aynı —
    # bu yüzden üst düzey satırın queryid'siyle aranıyor, metinle değil.
    queryid = await admin.fetchval(
        "SELECT s.queryid::text FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
        "WHERE r.rolname = 'dbace_it_app' AND s.toplevel AND s.query LIKE 'SELECT count(*) FROM orders WHERE status = $1%'"
    )
    rows = await admin.fetch(
        "SELECT s.toplevel, s.calls, s.query FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
        "WHERE r.rolname = 'dbace_it_app' AND s.queryid::text = $1",
        queryid,
    )
    log(await _version(admin), f"sunucu: {[(r['toplevel'], r['calls'], r['query'][:45]) for r in rows]}")
    entries = [e for e in (await _selection(instance.id, include_system=False)).entries if e.queryid == queryid]
    async with SessionLocal() as session:
        calls = await current_calls(session, instance.id, query=tag_query, queryid=queryid)
    log("seçim", f"{[(e.key, e.calls, e.sample.toplevel) for e in entries]} | eşik için sayılan çağrı={calls}")
    assert {r["toplevel"] for r in rows} == {True, False}
    keys = {e.key for e in entries}
    assert keys == {f"id:{queryid}", f"id:{queryid}:nested"}
    top = next(e for e in entries if e.key == f"id:{queryid}")
    assert top.calls == 1, "üst düzey fark iç içe çağrılarla karışmamalı"
    top_level_total = next(r["calls"] for r in rows if r["toplevel"])
    assert calls == top_level_total, "eşik üst düzey (uygulama) çağrısını saymalı"
