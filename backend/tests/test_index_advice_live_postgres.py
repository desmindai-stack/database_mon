"""Index önerisinin GERÇEK PostgreSQL'e karşı doğrulanması (Faz 31 İŞ 1).

Sahte bağlantı yok. Katalog cevapları (tablo var mı, kolon hangi tabloda, ifade IMMUTABLE mı,
planlayıcı önerilen index'i kullanıyor mu) yalnızca gerçek sunucudan alınabilir; bu özellik
sınıfında sahte katalogla yazılan testler üç tur boyunca yeşil kalıp canlıda çalışmayan bir
özelliği örtmüştü.

Çalıştırma:

    set DBACE_TEST_PG_DSN=postgresql://postgres:dbace@127.0.0.1:55432/dbace,postgresql://postgres:dbace@127.0.0.1:55433/dbace
    pytest tests/test_index_advice_live_postgres.py -v -s

DSN kullanıcısı yalnızca kurulum ve doğrulama için. dbace tarafı iki rolle koşuyor:
`dbace_it_super` (süper kullanıcı) ve `dbace_it_monitor` (yalnızca pg_monitor).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.collectors.base import ConnectionTarget
from app.collectors.query_marker import DBACE_QUERY_MARKER
from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample
from app.services import collection as collection_module
from app.services.credentials import encrypt_secret
from app.services.index_advice_watch import index_advice_watch_tick
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.slow_query_selection import select_slow_queries
from tests.live_pg import (
    LIVE_DSNS,
    NO_HYPOPG_DATABASE,
    SKIP_REASON,
    prepare_live_database,
    target_for,
    with_database,
)

asyncpg = pytest.importorskip("asyncpg")

_DSNS = LIVE_DSNS
pytestmark = pytest.mark.skipif(not _DSNS, reason=SKIP_REASON)

def log(title: str, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(params=_DSNS, ids=lambda d: d.rsplit("@", 1)[-1])
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


def _target(dsn: str, role: str = "super") -> ConnectionTarget:
    return target_for(dsn, role)


async def _advise(dsn, query, role="super", calls=1000):
    return await PostgreSQLIndexAdvisor(_target(dsn, role)).advise(query, calls, min_calls=5)


async def _version(admin) -> str:
    return await admin.fetchval("SELECT current_setting('server_version')")


def _preds(result):
    return {(p.column, p.kind, (f"{p.source.schema}.{p.source.table}" if p.source else None), p.usable) for p in result.predicates}


# --- 1a: sistem sorguları analize HİÇ girmiyor -----------------------------------------------


async def test_system_queries_never_reach_the_server(admin, dsn):
    """Kanıt: sorgular dbace rolüyle gönderiliyor ve pg_stat_statements'ta o rolden HİÇ kayıt yok
    — katalog araması bir yana, bağlantı bile açılmadı."""
    queries = [
        "SELECT relname FROM pg_class WHERE relkind = $1",
        "SELECT c.relname FROM pg_catalog.pg_class c WHERE c.relnamespace = $1",
        "SELECT table_name FROM information_schema.tables WHERE table_schema = $1",
        "SELECT pid FROM pg_stat_activity WHERE state = $1",
        f"{DBACE_QUERY_MARKER} SELECT count(*) FROM orders WHERE status = $1",
    ]
    await admin.execute("SELECT pg_stat_statements_reset()")
    statuses = [(q[:50], (await _advise(dsn, q)).status) for q in queries]
    rows = await admin.fetch(
        "SELECT s.query FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid WHERE r.rolname = 'dbace_it_super'"
    )
    log(await _version(admin), f"durumlar={statuses} | dbace rolünden pg_stat_statements kaydı={len(rows)}")
    assert all(status == "system" for _, status in statuses)
    assert rows == []


async def test_old_bug_public_pg_class_is_gone(admin, dsn):
    result = await _advise(dsn, "SELECT relname, relpages FROM pg_class WHERE relname = $1")
    messages = " ".join(r.message for r in result.reasons)
    assert "public.pg_class" not in messages
    assert result.status == "system"


async def test_unqualified_table_in_another_schema_is_found_in_the_catalog(admin, dsn):
    result = await _advise(dsn, "SELECT * FROM adv_invoices WHERE state = $1")
    log(await _version(admin), [(a.schema_name, a.index_ddl) for a in result.recommendations])
    assert result.recommendations and result.recommendations[0].schema_name == "adv_other"


async def test_table_in_two_schemas_is_not_guessed(admin, dsn):
    """Varsayılan şemada (public) YOK, iki başka şemada VAR: hangisi kullanılacağı bağlanan
    kullanıcının search_path'ine bağlı. (public'te olsaydı PostgreSQL onu seçerdi ve belirsizlik
    olmazdı — ilk yazılan test bu yanlış öncülle kırmızıya düştü.)"""
    result = await _advise(dsn, "SELECT * FROM adv_ledger WHERE amount > $1")
    ambiguous = [r for r in result.reasons if r.code == "ambiguous_table"]
    log(await _version(admin), ambiguous[0].message if ambiguous else result.reasons)
    assert ambiguous and "adv_other" in ambiguous[0].message and "adv_third" in ambiguous[0].message
    assert not result.recommendations


# --- 1b: $1 hipotezi ve kalıplar ------------------------------------------------------------


async def test_placeholders_and_literals_produce_the_same_index(admin, dsn):
    with_params = await _advise(dsn, "SELECT id FROM orders o WHERE o.status = $1 AND o.total > $2")
    with_literals = await _advise(dsn, "SELECT id FROM orders o WHERE o.status = 'paid' AND o.total > 100")
    ddl_params = [a.index_ddl for a in with_params.recommendations]
    ddl_literals = [a.index_ddl for a in with_literals.recommendations]
    log(await _version(admin), f"$1: {ddl_params} | literal: {ddl_literals}")
    assert ddl_params and ddl_params == ddl_literals


PATTERNS = {
    "where_eq_range": (
        "SELECT id FROM orders WHERE status = $1 AND created_at > $2",
        lambda r: any(a.columns == ["status", "created_at"] for a in r.recommendations),
    ),
    "unqualified_resolved_by_catalog": (
        "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE status = $1",
        lambda r: ("status", "eq", "public.orders", True) in _preds(r)
        and any(a.table_name == "orders" and "status" in a.columns for a in r.recommendations),
    ),
    "unqualified_ambiguous_reported": (
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id WHERE id = $1",
        lambda r: any(p.column == "id" and not p.usable and "birden çok tabloda" in (p.unusable_reason or "")
                      for p in r.predicates),
    ),
    "join_on_multi_condition": (
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id AND c.segment = $1",
        lambda r: any(a.table_name == "customers" and "segment" in a.columns for a in r.recommendations),
    ),
    "cte_inner_filter": (
        "WITH recent AS (SELECT * FROM orders WHERE created_at > $1) SELECT status, count(*) FROM recent GROUP BY status",
        lambda r: any(a.table_name == "orders" and "created_at" in a.columns for a in r.recommendations),
    ),
    "correlated_exists": (
        "SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = $1)",
        lambda r: any(a.table_name == "orders" and a.columns[:2] == ["status", "customer_id"] for a in r.recommendations),
    ),
    "in_subquery": (
        "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE segment = $1)",
        lambda r: any(a.table_name == "customers" and a.columns == ["segment"] for a in r.recommendations),
    ),
    "between_range": (
        "SELECT id FROM orders WHERE total BETWEEN $1 AND $2",
        lambda r: any(a.columns == ["total"] for a in r.recommendations),
    ),
    "like_prefix": (
        "SELECT id FROM adv_users WHERE name LIKE 'n12%'",
        lambda r: any(a.index_kind == "like_prefix" for a in r.recommendations),
    ),
    "like_unanchored_needs_pg_trgm": (
        "SELECT id FROM adv_users WHERE name LIKE '%12'",
        lambda r: any(p.column == "name" and not p.usable and "pg_trgm" in (p.unusable_reason or "") for p in r.predicates),
    ),
    "like_parameter_unknown": (
        "SELECT id FROM adv_users WHERE name LIKE $1",
        lambda r: any(p.kind == "like_unknown" and not p.usable for p in r.predicates),
    ),
    "already_indexed_primary_key": (
        "SELECT * FROM orders WHERE id = $1",
        lambda r: [x.code for x in r.reasons] == ["already_indexed"],
    ),
    "cast_to_date_on_timestamptz_is_not_immutable": (
        "SELECT id FROM orders WHERE created_at::date = $1",
        lambda r: any(p.expression and not p.usable and "IMMUTABLE" in (p.unusable_reason or "") for p in r.predicates),
    ),
}


@pytest.mark.parametrize("name", list(PATTERNS))
async def test_pattern_produces_advice_or_a_named_reason(admin, dsn, name):
    query, check = PATTERNS[name]
    result = await _advise(dsn, query)
    log(
        f"{await _version(admin)} {name}",
        f"status={result.status} öneri={[a.index_ddl for a in result.recommendations]} "
        f"sebep={[r.code for r in result.reasons]} filtreler={sorted(map(str, _preds(result)))}",
    )
    for pred in result.predicates:
        if not pred.usable:
            log("  dönüştürülemedi", f"{pred.column}: {pred.unusable_reason}")
    assert check(result)


# --- İfade index'i: önerilen DDL gerçekten çalışıyor ve planlayıcı onu kullanıyor -------------


@pytest.mark.parametrize(
    "query, probe",
    [
        (
            "SELECT id FROM adv_users u WHERE lower(u.email) = $1",
            "SELECT id FROM adv_users WHERE lower(email) = 'user42@example.com'",
        ),
        (
            "SELECT id FROM adv_events WHERE date_trunc('day', happened_at) = $1",
            "SELECT id FROM adv_events WHERE date_trunc('day', happened_at) = timestamp '2026-01-05'",
        ),
    ],
    ids=["lower_email", "date_trunc_timestamp"],
)
async def test_expression_index_ddl_executes_and_the_planner_uses_it(admin, dsn, query, probe):
    result = await _advise(dsn, query)
    expression = [a for a in result.recommendations if a.index_kind == "expression"]
    assert expression, f"ifade index'i önerilmedi: {[r.message for r in result.reasons]} {[p.unusable_reason for p in result.predicates]}"
    ddl = expression[0].index_ddl
    index_name = ddl.split()[2]
    await admin.execute(ddl)
    try:
        await admin.execute("ANALYZE")
        async with admin.transaction():
            # Seq scan kapatılıyor: ifade EŞLEŞMESEYDİ planlayıcı yine Seq Scan'e mecbur kalırdı.
            # Böylece test maliyet tesadüfüne değil, ifadenin eşleşip eşleşmediğine bakıyor.
            await admin.execute("SET LOCAL enable_seqscan = off")
            plan = await admin.fetchval(f"EXPLAIN (FORMAT JSON) {probe}")
        plan_text = str(plan)
        log(await _version(admin), f"DDL={ddl} | plan index kullanıyor={index_name in plan_text}")
        assert index_name in plan_text
    finally:
        await admin.execute(f"DROP INDEX IF EXISTS {index_name}")


async def test_stable_expression_is_not_recommended_and_says_why(admin, dsn):
    result = await _advise(dsn, "SELECT id FROM orders WHERE date_trunc('day', created_at) = $1")
    pred = next(p for p in result.predicates if p.expression)
    log(await _version(admin), pred.unusable_reason)
    assert not any(a.index_kind == "expression" for a in result.recommendations)
    assert not pred.usable and "IMMUTABLE" in pred.unusable_reason


# --- Yetki: yalnızca pg_monitor rolü -----------------------------------------------------------


async def test_monitor_role_gets_measurement_notes_not_fabricated_numbers(admin, dsn):
    result = await _advise(dsn, "SELECT id FROM orders WHERE status = $1 AND created_at > $2", role="monitor")
    log(
        await _version(admin),
        f"status={result.status} öneri={[(a.index_ddl, a.estimated_improvement_pct, a.measurement_notes) for a in result.recommendations]} "
        f"sebep={[r.message for r in result.reasons]}",
    )
    assert result.recommendations, "yapısal öneri yetki olmadan da üretilebilmeli"
    advice = result.recommendations[0]
    assert advice.estimated_improvement_pct is None, "ölçülemeyen fayda için yüzde uydurulmamalı"
    assert any("SELECT" in note and "yetki" in note for note in advice.measurement_notes)
    assert set(result.required_grants) == {"public.orders"}
    assert len(result.required_grants["public.orders"]) == 2, "istatistik denetimi ve fayda ölçümü — iki ayrı neden"


async def test_monitor_role_expression_index_is_verified_by_hypopg_without_select_or_temp(admin, dsn):
    """Faz 31 Commit 8: IMMUTABLE denetimi geçici tabloda CREATE INDEX ile yapılıyordu — bankada izleme
    kullanıcısının TEMP/CREATE yetkisi yok. hypopg aynı denetimi SELECT ve TEMP olmadan yapıyor (ölçüldü).
    pg_monitor rolünün adv_users'ta SELECT'i yok: öneri DOĞRULANMIŞ, gereken tek yetki fayda ölçümü için."""
    from app.services.index_advice_watch import report_payload
    from app.services.index_advisor import GRANT_REASON_BENEFIT

    result = await _advise(dsn, "SELECT id FROM adv_users WHERE lower(email) = $1", role="monitor")
    expression = [a for a in result.recommendations if a.index_kind == "expression"]
    grants = report_payload(result, watch=None, watch_enabled=True)["required_grants"]
    log(await _version(admin), f"öneri={[(a.index_ddl, a.verified) for a in expression]} yetkiler={grants}")
    assert len(expression) == 1 and expression[0].verified is True and expression[0].verification_note is None
    assert grants["tables"] == [{"table": "public.adv_users", "privilege": "SELECT", "reasons": [GRANT_REASON_BENEFIT]}]
    assert "TEMPORARY" not in str(grants)


async def test_without_hypopg_expression_index_is_unverified_and_asks_for_hypopg_not_temp(admin, dsn):
    no_hypopg = await asyncpg.connect(with_database(dsn, NO_HYPOPG_DATABASE), statement_cache_size=0)
    try:
        await prepare_live_database(no_hypopg)
    finally:
        await no_hypopg.close()
    result = await PostgreSQLIndexAdvisor(target_for(dsn, "monitor", database=NO_HYPOPG_DATABASE)).advise(
        "SELECT id FROM adv_users WHERE lower(email) = $1", 1000, min_calls=5)
    expression = [a for a in result.recommendations if a.index_kind == "expression"]
    log(await _version(admin), f"öneri={[(a.index_ddl, a.verified) for a in expression]} not={expression[0].verification_note[:120] if expression else None!r}")
    assert len(expression) == 1 and expression[0].verified is False
    note = expression[0].verification_note
    assert note.startswith("DOĞRULANMADI") and "hypopg" in note and "TEMP/CREATE yetkisi İSTENMİYOR" in note


async def test_super_role_expression_index_is_verified_and_needs_no_grants(admin, dsn):
    result = await _advise(dsn, "SELECT id FROM adv_users WHERE lower(email) = $1", role="super")
    expression = [a for a in result.recommendations if a.index_kind == "expression"]
    assert len(expression) == 1 and expression[0].verified is True and expression[0].verification_note is None
    assert result.required_grants == {}


# --- API → servis → PG: toplu öneri, eşik izleme, ayar, dbace'in kendi sorguları --------------


async def _instance(dsn, role="super") -> Instance:
    target = _target(dsn, role)
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"advice-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=target.host, port=target.port,
            database=target.database, username=target.username, password=encrypt_secret(target.password),
            enabled=True,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance


async def test_batch_endpoint_counts_unparsable_and_other_outcomes(admin, dsn):
    from tests.auth_helper import authed_client

    instance = await _instance(dsn)
    items = [
        {"query": "SELECT id FROM orders WHERE status = $1 AND created_at > $2", "calls": 1000},
        # PostgreSQL'in KABUL ettiği ama sqlglot'un ayrıştıramadığı gerçek sözdizimi (ORDER BY ... USING).
        # Parantezi kapanmamış bir metin "kesik" sayılırdı — o başka bir sayaç.
        {"query": "SELECT id FROM orders WHERE status = $1 ORDER BY id USING >", "calls": 1000},
        {"query": "SELECT relname FROM pg_class WHERE relkind = $1", "calls": 1000},
        {"query": f"SELECT id FROM adv_users WHERE name = 'batch-{uuid.uuid4().hex[:6]}' AND email = $1", "calls": 2},
    ]
    async with await authed_client() as client:
        response = await client.post(f"/api/queries/{instance.id}/advice/batch", json={"items": items})
    assert response.status_code == 200, response.text
    body = response.json()
    log(await _version(admin), body["summary"])
    counts = body["summary"]["counts"]
    assert counts["unparsable"] == 1 and counts["advised"] == 1 and counts["system"] == 1
    assert counts["below_threshold"] == 1
    assert "4 sorgudan" in body["summary"]["text"] and "1'i çözümlenemedi" in body["summary"]["text"]
    below = next(i for i in body["items"] if i["report"]["status"] == "below_threshold")
    assert below["report"]["threshold"] == {
        "calls_now": 2, "threshold": 5, "watching": True, "watch_enabled": True,
        "watch_id": below["report"]["watch"]["id"],
    }
    assert "2/5" in below["report"]["no_advice_reasons"][0]["message"]
    assert below["report"]["predicates"], "eşik altında da bulunan filtreler gösterilmeli"


async def test_watch_is_registered_then_the_real_tick_produces_advice_when_the_threshold_is_met(admin, dsn):
    """Uçtan uca: API izlemeye alıyor → toplama döngüsü yeni çağrı sayısını yazıyor →
    zamanlayıcının GERÇEK tur fonksiyonu öneriyi üretiyor → API "hazır" gösteriyor."""
    from tests.auth_helper import authed_client

    instance = await _instance(dsn)
    queryid = f"it-{uuid.uuid4().hex[:10]}"
    query = f"SELECT id FROM orders WHERE status = $1 AND total > $2 /* {queryid} */"
    async with SessionLocal() as session:
        session.add(SlowQuerySample(instance_id=instance.id, queryid=queryid, query=query, calls=2,
                                    total_time_ms=10, mean_time_ms=5, rows=1, collected_at=datetime.now(UTC)))
        await session.commit()

    async with await authed_client() as client:
        first = (await client.post(f"/api/queries/{instance.id}/advice",
                                   json={"query": query, "queryid": queryid, "calls": 999})).json()
        # İstemcinin 999 demesine rağmen sunucu kendi kümülatif verisini (2) kullanıyor.
        assert first["status"] == "below_threshold", first
        assert first["threshold"]["calls_now"] == 2

        watches = (await client.get(f"/api/queries/{instance.id}/advice-watches")).json()
        assert [w["status"] for w in watches] == ["waiting"]

        async with SessionLocal() as session:
            session.add(SlowQuerySample(instance_id=instance.id, queryid=queryid, query=query, calls=7,
                                        total_time_ms=30, mean_time_ms=4, rows=1, collected_at=datetime.now(UTC)))
            await session.commit()

        totals = await index_advice_watch_tick()
        log(await _version(admin), f"tur={totals}")

        watches = (await client.get(f"/api/queries/{instance.id}/advice-watches")).json()
        log("izleme", {k: watches[0][k] for k in ("status", "calls_seen", "threshold")})
        assert watches[0]["status"] == "ready"
        assert watches[0]["calls_seen"] == 7
        assert watches[0]["report"]["status"] == "advised"
        assert watches[0]["report"]["advice"][0]["table_name"] == "orders"


async def test_threshold_setting_changes_the_outcome_through_the_api(admin, dsn):
    from tests.auth_helper import authed_client

    instance = await _instance(dsn)
    query = f"SELECT id FROM orders WHERE customer_id = $1 /* thr-{uuid.uuid4().hex[:6]} */"
    async with await authed_client() as client:
        try:
            put = await client.put("/api/admin/analysis-settings", json={"index_advice_min_calls": 50})
            assert put.status_code == 200 and put.json()["index_advice_min_calls"] == 50
            report = (await client.post(f"/api/queries/{instance.id}/advice", json={"query": query, "calls": 20})).json()
            assert report["status"] == "below_threshold" and report["threshold"]["threshold"] == 50

            await client.put("/api/admin/analysis-settings", json={"index_advice_min_calls": 10})
            report = (await client.post(f"/api/queries/{instance.id}/advice", json={"query": query + " ", "calls": 20})).json()
            log(await _version(admin), f"eşik 10, çağrı 20 → {report['status']}")
            assert report["status"] == "advised"
            bad = await client.put("/api/admin/analysis-settings", json={"index_advice_min_calls": 0})
            assert bad.status_code == 422
        finally:
            await client.put("/api/admin/analysis-settings", json={"index_advice_min_calls": 5})


async def test_slow_query_list_filters_dbaces_own_marked_queries(admin, dsn):
    """Commit 1'in imzası burada tüketiliyor: gerçek toplama döngüsünden sonra liste dbace'in
    kendi sorgularını göstermiyor, `show_system_queries` açıkken gösteriyor."""
    instance = await _instance(dsn)
    await admin.execute("SELECT pg_stat_statements_reset()")
    await admin.fetchval("SELECT count(*) FROM orders WHERE status = 'paid'")  # uygulama sorgusu
    async with SessionLocal() as session:
        instance = await session.get(Instance, instance.id)
        await collection_module.collect_instance(instance, session)
        await collection_module.collect_instance(instance, session)
        await session.commit()
        hidden = await select_slow_queries(session, instance.id, include_system=False, min_total_ms=0, min_calls=0, limit=500)
        shown = await select_slow_queries(session, instance.id, include_system=True, min_total_ms=0, min_calls=0, limit=500)
    marked_hidden = [e for e in hidden.entries if DBACE_QUERY_MARKER in e.query]
    marked_shown = [e for e in shown.entries if DBACE_QUERY_MARKER in e.query]
    log(await _version(admin), f"gizli modda imzalı={len(marked_hidden)} filtrelenen sistem={hidden.filtered_system} | göster modunda imzalı={len(marked_shown)}")
    assert marked_hidden == []
    assert marked_shown, "toplama dbace imzalı sorgu üretmeliydi — test bir şey kanıtlamıyor"
    assert hidden.filtered_system >= len(marked_shown)



# --- hypopg'SUZ yol (Faz 31 Commit 4) ---------------------------------------------------------
#
# Bankacılık ortamında hypopg büyük olasılıkla KURULU OLMAYACAK (üçüncü taraf eklenti, onay
# süreci). Aynı sunucuda eklenti veritabanı başına kurulduğu için hypopg'siz yol ayrı bir
# veritabanında (`dbace_nohypopg`, scripts/live_pg.py) ve aynı sürümde sınanıyor.


@pytest.mark.parametrize(
    "query, index_kind",
    [
        ("SELECT id FROM orders WHERE status = $1 AND created_at > $2", "btree"),
        ("SELECT id FROM adv_users u WHERE lower(u.email) = $1", "expression"),
    ],
    ids=["btree", "ifade"],
)
async def test_without_hypopg_advice_is_an_estimate_and_says_so(admin, dsn, query, index_kind):
    no_hypopg = await asyncpg.connect(with_database(dsn, NO_HYPOPG_DATABASE), statement_cache_size=0)
    try:
        await prepare_live_database(no_hypopg)
        assert not await no_hypopg.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'hypopg')")
    finally:
        await no_hypopg.close()

    with_ext = await PostgreSQLIndexAdvisor(target_for(dsn, "super")).advise(query, 1000, min_calls=5)
    without = await PostgreSQLIndexAdvisor(target_for(dsn, "super", database=NO_HYPOPG_DATABASE)).advise(query, 1000, min_calls=5)

    a = next(x for x in with_ext.recommendations if x.index_kind == index_kind)
    b = next(x for x in without.recommendations if x.index_kind == index_kind)
    log(
        f"{await _version(admin)} {index_kind}",
        f"hypopg VAR: %{a.estimated_improvement_pct} ölçüm={a.has_hypopg_estimate} maliyet={a.before_cost}→{a.after_cost} | "
        f"hypopg YOK: %{b.estimated_improvement_pct} ölçüm={b.has_hypopg_estimate} "
        f"notlar={b.measurement_notes}",
    )
    assert a.index_ddl == b.index_ddl, "öneri hypopg'nin varlığından bağımsız aynı olmalı"
    assert a.has_hypopg_estimate and a.before_cost and a.after_cost
    assert not b.has_hypopg_estimate and b.before_cost is None and b.after_cost is None
    assert any("hypopg kurulu değil" in note and "ÖLÇÜLMEDİ" in note for note in b.measurement_notes)
    # Ölçüm yoksa YÜZDE YOK: istatistik formülü ölçülen faydadan 2,6 kat sapıyordu (%88,6 / %34).
    assert b.estimated_improvement_pct is None
    if index_kind == "btree":
        # Faz 31 Commit 5: seçicilik yüzdesi de yok; aralık kolonu için gerekçe kalıyor.
        assert not hasattr(b, "estimated_selectivity_pct")
        assert not any("%" in note for note in b.measurement_notes), b.measurement_notes
        assert any("Aralık filtreleri (created_at)" in note for note in b.measurement_notes)
