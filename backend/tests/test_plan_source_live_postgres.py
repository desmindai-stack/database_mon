"""Plan kaynağı önceliklendirmesinin GERÇEK PostgreSQL'e karşı doğrulanması (Faz 31 İŞ 2).

Sahte bağlantı yok. Her kaynak gerçek yolundan üretiliyor:

* **auto_explain planı** — konteynerde oturum düzeyinde `auto_explain` açılıp sorgu
  çalıştırılıyor, sunucu log'u `docker logs` ile okunup GERÇEK ayrıştırıcı ve kayıt
  fonksiyonlarından geçiriliyor (`parse_auto_explain_log`, `store_captured_plans`). Atlanan
  tek halka host-agent'ın HTTP taşıması.
* **Gerçek değerli örnek** — gerçek bekleme örnekleyicisi (`wait_sampling._sample_instance`)
  sorgu çalışırken döndürülüyor, kovalar gerçek yazma yolundan (`flush_all_buckets`) yazılıyor.
* **Yavaş sorgu satırı** — gerçek toplama döngüsü (`collection.collect_instance`).
* **Uçlar** — FastAPI uygulaması üzerinden, kimlik doğrulamalı.

Çalıştırma:

    set DBACE_TEST_PG_DSN=postgresql://postgres:dbace@127.0.0.1:55432/dbace,postgresql://postgres:dbace@127.0.0.1:55433/dbace
    pytest tests/test_plan_source_live_postgres.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from sqlalchemy import select

from app.collectors.base import ConnectionTarget
from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample, WaitQuerySignature
from app.services import collection as collection_module
from app.services import explain_service as explain_module
from app.services import wait_sampling
from app.services.auto_explain import parse_auto_explain_log
from app.services.credentials import encrypt_secret
from app.services.plan_capture import store_captured_plans

asyncpg = pytest.importorskip("asyncpg")

_DSNS = [d.strip() for d in os.environ.get("DBACE_TEST_PG_DSN", "").split(",") if d.strip()]
pytestmark = pytest.mark.skipif(not _DSNS, reason="Gerçek PostgreSQL yok. DBACE_TEST_PG_DSN tanımlayın.")

ROLE_PASSWORD = "dbace_it_pw"
ROLES = {"super": ("dbace_it_super", "SUPERUSER"), "monitor": ("dbace_it_monitor", "IN ROLE pg_monitor")}


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(params=_DSNS, ids=lambda d: d.rsplit("@", 1)[-1])
def dsn(request):
    return request.param


@pytest.fixture
async def admin(dsn):
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    for name, attrs in ROLES.values():
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name):
            await conn.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{ROLE_PASSWORD}' {attrs}")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id bigserial PRIMARY KEY, customer_id bigint NOT NULL, status text NOT NULL,
            total numeric NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS ps_write_probe (id bigserial PRIMARY KEY, note text);
        CREATE OR REPLACE FUNCTION ps_writer() RETURNS int LANGUAGE sql AS
            $$ INSERT INTO ps_write_probe (note) VALUES ('yazıldı') RETURNING 1 $$;
        """
    )
    if not await conn.fetchval("SELECT count(*) FROM orders"):
        await conn.execute(
            "INSERT INTO orders (customer_id, status, total, created_at) SELECT (g % 2000) + 1, "
            "CASE WHEN g % 5 = 0 THEN 'paid' ELSE 'new' END, (g % 900)::numeric, now() - (g || ' minutes')::interval "
            "FROM generate_series(1, 50000) g"
        )
        await conn.execute("ANALYZE orders")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _clean_sampler_state():
    yield
    for sampler in list(wait_sampling._samplers.values()):
        await wait_sampling._drop_connection(sampler)
    wait_sampling.reset_state()


@pytest.fixture
async def client():
    from tests.auth_helper import authed_client

    async with await authed_client() as c:
        await c.put("/api/admin/analysis-settings", json={"store_real_query_samples": False})
        try:
            yield c
        finally:
            await c.put("/api/admin/analysis-settings", json={"store_real_query_samples": False})


def _target(dsn: str, role: str) -> ConnectionTarget:
    url = urlparse(dsn)
    return ConnectionTarget(
        host=url.hostname or "127.0.0.1", port=url.port or 5432,
        database=(url.path or "/postgres").lstrip("/"), username=ROLES[role][0], password=ROLE_PASSWORD,
    )


async def _version(admin) -> str:
    return (await admin.fetchval("SELECT current_setting('server_version')")).split(" ")[0]


async def _instance(dsn: str, role: str = "super") -> Instance:
    target = _target(dsn, role)
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"plansrc-it-{role}-{uuid.uuid4().hex[:8]}", engine="postgresql", host=target.host,
            port=target.port, database=target.database, username=target.username,
            password=encrypt_secret(target.password), enabled=True,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance


async def _collect(instance_id: int) -> None:
    # Yavaş sorgu toplaması instance başına seyreltiliyor (Faz 21 İŞ 3). Test aynı instance'ı
    # ard arda topluyor; aralık dolmuş gibi kaydı temizliyoruz — zamanlayıcının aralık
    # sonunda gördüğü durumun aynısı.
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        instance = await session.get(Instance, instance_id)
        await collection_module.collect_instance(instance, session)
        await session.commit()


async def _slow_query_row(admin, instance_id: int, marker: str) -> SlowQuerySample:
    queryid = await admin.fetchval(
        "SELECT queryid::text FROM pg_stat_statements WHERE query LIKE $1 AND query NOT LIKE '%pg_stat_statements%' "
        "ORDER BY calls DESC LIMIT 1",
        f"%{marker}%",
    )
    assert queryid, f"pg_stat_statements'ta {marker} yok"
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(SlowQuerySample)
                .where(SlowQuerySample.instance_id == instance_id, SlowQuerySample.queryid == queryid)
                .order_by(SlowQuerySample.collected_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    assert row is not None, "toplama döngüsü sorguyu yazmadı"
    return row


async def _run_with_sampler(instance_id: int, run) -> None:
    """Sorgu çalışırken GERÇEK örnekleyiciyi döndürür, sonra kovaları gerçek yoldan yazar."""
    async with SessionLocal() as session:
        instance = await session.get(Instance, instance_id)
    done = asyncio.Event()

    async def sampler_loop():
        while not done.is_set():
            await wait_sampling._sample_instance(instance, datetime.now(UTC))
            await asyncio.sleep(0.15)

    task = asyncio.create_task(sampler_loop())
    try:
        await run()
    finally:
        done.set()
        await task
    async with SessionLocal() as session:
        await wait_sampling.flush_all_buckets(session)
        await session.commit()


async def _signature(instance_id: int, queryid: str) -> WaitQuerySignature | None:
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(WaitQuerySignature).where(
                    WaitQuerySignature.instance_id == instance_id, WaitQuerySignature.queryid == queryid
                )
            )
        ).scalar_one_or_none()


def _slow_sql(marker: str, secret: str, seconds: float) -> str:
    # pg_sleep bir alt sorguda: satır başına değil, BİR KEZ çalışır.
    return (
        f"SELECT count(*) AS {marker} FROM orders WHERE status = '{secret}' "
        f"AND (SELECT pg_sleep({seconds})) IS NOT NULL"
    )


async def _options(client, instance_id: int, sample_id: int) -> dict:
    response = await client.get(f"/api/queries/{instance_id}/plan-sources", params={"sample_id": sample_id})
    assert response.status_code == 200, response.text
    body = response.json()
    body["by_kind"] = {o["kind"]: o for o in body["options"]}
    return body


async def _prepare_sample(admin, client, dsn, *, store: bool, role: str = "super"):
    """Gerçek iş yükü + örnekleyici + toplama. (instance, yavaş sorgu satırı, marker, sır)."""
    marker = f"ps_{uuid.uuid4().hex[:8]}"
    instance = await _instance(dsn, role)
    await client.put("/api/admin/analysis-settings", json={"store_real_query_samples": store})
    await admin.execute("SELECT pg_stat_statements_reset()")
    fast_secret, slow_secret = f"secret-fast-{uuid.uuid4().hex[:6]}", f"secret-slow-{uuid.uuid4().hex[:6]}"

    async def workload():
        await admin.execute(_slow_sql(marker, fast_secret, 0.6))
        await admin.execute(_slow_sql(marker, slow_secret, 1.6))

    await _run_with_sampler(instance.id, workload)
    await _collect(instance.id)
    row = await _slow_query_row(admin, instance.id, marker)
    return instance, row, marker, fast_secret, slow_secret


# --- 3.3: gizlilik ve örnekleme --------------------------------------------------------------


async def test_default_off_stores_normalized_text_and_no_sample(admin, client, dsn):
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=False)
    sig = await _signature(instance.id, row.queryid)
    log(await _version(admin), f"query_text={sig.query_text!r} sample={sig.sample_query_text!r}")
    assert sig is not None
    assert fast not in sig.query_text and slow not in sig.query_text
    assert sig.sample_query_text is None and sig.sample_duration_ms is None
    sources = await _options(client, instance.id, row.id)
    assert not sources["by_kind"]["sample_analyze"]["available"]
    assert "KAPALI" in sources["by_kind"]["sample_analyze"]["reason"]


async def test_enabled_keeps_the_slowest_real_execution(admin, client, dsn):
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=True)
    sig = await _signature(instance.id, row.queryid)
    log(await _version(admin), f"örnek={sig.sample_query_text!r} süre={sig.sample_duration_ms} ms")
    assert slow in sig.sample_query_text, "en yavaş çalıştırma (1.6 sn) örnek olmalıydı"
    assert sig.sample_duration_ms >= 900
    # Ayar AÇIKKEN bile sözlük metni değersiz: yük kırılımı ve teknik rapor bu alanı gösteriyor.
    assert slow not in sig.query_text and fast not in sig.query_text


async def test_turning_off_deletes_samples_and_normalizes_text(admin, client, dsn):
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=True)
    assert (await _signature(instance.id, row.queryid)).sample_query_text
    response = await client.put("/api/admin/analysis-settings", json={"store_real_query_samples": False})
    assert response.status_code == 200 and response.json()["store_real_query_samples"] is False
    sig = await _signature(instance.id, row.queryid)
    log(await _version(admin), f"kapatma sonrası query_text={sig.query_text!r} sample={sig.sample_query_text!r}")
    assert sig.sample_query_text is None and sig.sample_duration_ms is None and sig.sample_captured_at is None
    assert slow not in sig.query_text and fast not in sig.query_text


async def test_bind_parameter_application_gets_no_sample_and_is_told_why(admin, client, dsn):
    marker = f"ps_{uuid.uuid4().hex[:8]}"
    instance = await _instance(dsn)
    await client.put("/api/admin/analysis-settings", json={"store_real_query_samples": True})
    await admin.execute("SELECT pg_stat_statements_reset()")
    app = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        async def workload():
            await app.fetch(
                f"SELECT count(*) AS {marker} FROM orders WHERE status = $1 AND (SELECT pg_sleep($2::float)) IS NOT NULL",
                "gizli-deger", 1.2,
            )

        await _run_with_sampler(instance.id, workload)
    finally:
        await app.close()
    await _collect(instance.id)
    row = await _slow_query_row(admin, instance.id, marker)
    sig = await _signature(instance.id, row.queryid)
    reason = (await _options(client, instance.id, row.id))["by_kind"]["sample_analyze"]["reason"]
    log(await _version(admin), f"görülen metin={sig.query_text!r} bind işareti={sig.seen_bind_parameters} | sebep={reason[:120]}")
    assert sig.sample_query_text is None and sig.seen_bind_parameters is True
    assert "PARAMETRELİ" in reason


# --- 3.1 / 3.2: öncelik ve auto_explain ------------------------------------------------------


def _container_for(dsn: str) -> str:
    if not shutil.which("docker"):
        pytest.skip("docker CLI yok — auto_explain log'u okunamıyor")
    port = urlparse(dsn).port
    name = subprocess.run(
        ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not name:
        pytest.skip(f"{port} portunu yayınlayan konteyner bulunamadı")
    return name


async def _capture_real_auto_explain_plan(admin, dsn, instance_id: int, sql: str) -> int:
    container = _container_for(dsn)
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    time.sleep(0.05)
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await conn.execute("LOAD 'auto_explain'")
        await conn.execute("SET auto_explain.log_min_duration = 0")
        await conn.execute("SET auto_explain.log_format = 'json'")
        await conn.execute("SET auto_explain.log_analyze = on")
        await conn.execute(sql)
    finally:
        await conn.close()
    time.sleep(0.3)
    lines = subprocess.run(
        ["docker", "logs", "--since", since, container], capture_output=True, text=True, encoding="utf-8",
    )
    parsed = parse_auto_explain_log((lines.stdout + lines.stderr).splitlines())
    async with SessionLocal() as session:
        written = await store_captured_plans(session, instance_id, parsed.plans)
        await session.commit()
    return written


async def test_captured_plan_wins_and_no_explain_is_sent(admin, client, dsn):
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=True)
    written = await _capture_real_auto_explain_plan(admin, dsn, instance.id, _slow_sql(marker, "secret-ae", 0.01))
    assert written >= 1, "auto_explain planı yakalanamadı"

    await admin.execute("SELECT pg_stat_statements_reset()")
    sources = await _options(client, instance.id, row.id)
    plan_id = sources["by_kind"]["captured"]["detail"]["plan_id"]
    plan = (await client.get(f"/api/queries/{instance.id}/captured-plans/{plan_id}")).json()
    dbace_rows = await admin.fetch(
        "SELECT s.query FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid WHERE r.rolname = 'dbace_it_super'"
    )
    log(
        await _version(admin),
        f"önerilen={sources['recommended']} sıra={[(o['kind'], o['available']) for o in sources['options']]} "
        f"plan kaynağı={plan['source_label']!r} gerçek satır={plan['analysis'] is not None} | "
        f"dbace rolünden pg_stat_statements kaydı={len(dbace_rows)}",
    )
    assert sources["recommended"] == "captured"
    assert [o["kind"] for o in sources["options"]] == ["captured", "sample_analyze", "generic", "unavailable"]
    assert plan["source"] == "auto_explain" and "Gerçek çalıştırmadan yakalandı" in plan["source_label"]
    assert dbace_rows == [], "yakalanmış plan varken hedef sunucuya hiçbir sorgu gönderilmemeli"


async def test_sample_analyze_through_the_api_returns_actual_rows_and_is_labelled(admin, client, dsn):
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=True)
    sources = await _options(client, instance.id, row.id)
    assert sources["recommended"] == "sample_analyze", sources["by_kind"]["captured"]["reason"]
    assert "sample_query_text" not in str(sources) and slow not in str(sources), "GET yanıtı örnek metni taşımamalı"
    response = await client.post(
        f"/api/queries/{instance.id}/explain",
        json={"query": row.query, "use_sample": True, "sample_id": row.id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    log(await _version(admin), f"kaynak={body['source_label']!r} süre={body['execution_time_ms']} ms uyarı={body['source_caveat'][:90]!r}")
    assert body["source"] == "sample_analyze" and body["analyzed"] is True
    assert "Actual Rows" in str(body["raw_plan"])
    assert slow in body["query"]


# --- 3.4: güvenlik katmanları ----------------------------------------------------------------


async def _set_sample(instance_id: int, queryid: str, text: str) -> None:
    async with SessionLocal() as session:
        sig = (
            await session.execute(
                select(WaitQuerySignature).where(
                    WaitQuerySignature.instance_id == instance_id, WaitQuerySignature.queryid == queryid
                )
            )
        ).scalar_one()
        sig.sample_query_text = text
        sig.sample_duration_ms = 1000.0
        sig.sample_captured_at = datetime.now(UTC)
        await session.commit()


async def _explain_sample(client, instance_id, row):
    return await client.post(
        f"/api/queries/{instance_id}/explain", json={"query": row.query, "use_sample": True, "sample_id": row.id}
    )


@pytest.mark.parametrize(
    "text, expect",
    [
        ("DELETE FROM orders WHERE id = 1", "EXPLAIN"),
        ("WITH d AS (DELETE FROM orders WHERE id = 1 RETURNING *) SELECT * FROM d", "DML"),
        ("SHOW work_mem", "yalnızca veri okuyan"),
        ("SELECT id FROM orders WHERE id = 1 FOR UPDATE", "DML"),
    ],
    ids=["delete", "dml-in-cte", "show", "for-update"],
)
async def test_non_select_samples_are_refused_before_reaching_the_server(admin, client, dsn, text, expect):
    instance, row, *_ = await _prepare_sample(admin, client, dsn, store=True)
    await _set_sample(instance.id, row.queryid, text)
    before = await admin.fetchval("SELECT count(*) FROM orders")
    response = await _explain_sample(client, instance.id, row)
    log(await _version(admin), f"{text[:40]!r} → {response.status_code} {response.json()['detail'][:100]!r}")
    assert response.status_code == 400 and expect in response.json()["detail"]
    assert await admin.fetchval("SELECT count(*) FROM orders") == before


@pytest.mark.parametrize(
    "text",
    # Metin denetimi ikisini de GÖREMİYOR (SELECT ile başlıyor, DML kelimesi yok). nextval
    # ayrıca ROLLBACK'in geri ALAMADIĞI bir yan etki: dizi geri sarılmaz — tek engel
    # salt-okunur işlem.
    ["SELECT ps_writer()", "SELECT nextval('orders_id_seq')"],
    ids=["writing-function", "nextval"],
)
async def test_read_only_transaction_stops_what_the_text_check_cannot_see(admin, client, dsn, text):
    instance, row, *_ = await _prepare_sample(admin, client, dsn, store=True)
    await _set_sample(instance.id, row.queryid, text)
    before_rows = await admin.fetchval("SELECT count(*) FROM ps_write_probe")
    before_seq = await admin.fetchval("SELECT last_value FROM orders_id_seq")
    response = await _explain_sample(client, instance.id, row)
    detail = response.json()["detail"]
    log(await _version(admin), f"{text!r} → {response.status_code} {detail[:140]!r}")
    assert response.status_code == 400 and "read-only transaction" in detail
    assert await admin.fetchval("SELECT count(*) FROM ps_write_probe") == before_rows
    assert await admin.fetchval("SELECT last_value FROM orders_id_seq") == before_seq


async def test_statement_timeout_cancels_a_long_analyze(admin, client, dsn, monkeypatch):
    monkeypatch.setattr(explain_module, "ANALYZE_STATEMENT_TIMEOUT_MS", 700)
    instance, row, *_ = await _prepare_sample(admin, client, dsn, store=True)
    await _set_sample(instance.id, row.queryid, "SELECT pg_sleep(10)")
    started = time.monotonic()
    response = await _explain_sample(client, instance.id, row)
    elapsed = time.monotonic() - started
    log(await _version(admin), f"pg_sleep(10) → {response.status_code} {elapsed:.2f} sn {response.json()['detail'][:80]!r}")
    assert response.status_code == 400 and elapsed < 5
    detail = response.json()["detail"]
    # ANALYZE yolunda zaman aşımı ÇALIŞTIRMA sınırı — "planlama uzun sürdü" demek yanıltıcıydı.
    assert "çalıştırma sınırını aştı" in detail and "geri alındı" in detail


async def test_analyze_always_rolls_back_measured_in_pg_stat_statements(admin, client, dsn):
    instance, row, *_ = await _prepare_sample(admin, client, dsn, store=True)
    await admin.execute("SELECT pg_stat_statements_reset()")
    assert (await _explain_sample(client, instance.id, row)).status_code == 200
    rows = await admin.fetch(
        "SELECT s.query, s.calls FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
        "WHERE r.rolname = 'dbace_it_super' AND s.toplevel ORDER BY s.query"
    )
    texts = [r["query"] for r in rows]
    log(await _version(admin), texts)
    assert any("BEGIN READ ONLY" in t for t in texts)
    assert any(t.strip().endswith("ROLLBACK") or "ROLLBACK;" in t for t in texts)
    assert not any("COMMIT" in t for t in texts), "ANALYZE işlemi hiçbir koşulda commit edilmemeli"
    assert any("SET LOCAL statement_timeout" in t for t in texts)


async def test_viewer_cannot_run_analyze(admin, client, dsn):
    from tests.auth_helper import authed_client

    instance, row, *_ = await _prepare_sample(admin, client, dsn, store=True)
    async with await authed_client("viewer") as viewer:
        sources = await viewer.get(f"/api/queries/{instance.id}/plan-sources", params={"sample_id": row.id})
        response = await viewer.post(
            f"/api/queries/{instance.id}/explain", json={"query": row.query, "use_sample": True, "sample_id": row.id}
        )
    assert sources.status_code == 200
    assert response.status_code == 403


async def test_manual_analyze_uses_the_same_safe_path(admin, client, dsn):
    """Faz 31 öncesinde elle EXPLAIN ANALYZE işlemsiz çalışıyordu; artık aynı güvenli yol."""
    instance = await _instance(dsn)
    await _collect(instance.id)
    response = await client.post(
        f"/api/queries/{instance.id}/explain", json={"query": "SELECT ps_writer()", "analyze": True}
    )
    log(await _version(admin), f"elle ANALYZE ps_writer() → {response.status_code} {response.json().get('detail', '')[:90]!r}")
    assert response.status_code == 400 and "read-only transaction" in response.json()["detail"]


# --- 3.9: yalnızca pg_monitor ----------------------------------------------------------------


async def test_monitor_role_sample_analyze_is_explained_not_empty(admin, client, dsn):
    instance, row, *_ = await _prepare_sample(admin, client, dsn, store=True, role="monitor")
    sig = await _signature(instance.id, row.queryid)
    response = await _explain_sample(client, instance.id, row)
    detail = response.json().get("detail", "")
    log(await _version(admin), f"örnek saklandı={sig.sample_query_text is not None} → {response.status_code} {detail[:120]!r}")
    assert sig.sample_query_text, "pg_monitor (pg_read_all_stats) başka rolün sorgu metnini görebilmeli"
    assert response.status_code == 400 and "yetki" in detail


# --- 3.5: sürüm farkı ------------------------------------------------------------------------


async def test_generic_fallback_per_version(admin, client, dsn):
    instance = await _instance(dsn)
    await admin.execute("SELECT pg_stat_statements_reset()")
    marker = f"ps_{uuid.uuid4().hex[:8]}"
    await admin.execute(f"SELECT count(*) AS {marker} FROM orders WHERE status = 'paid' AND total > 10")
    await _collect(instance.id)
    row = await _slow_query_row(admin, instance.id, marker)
    sources = await _options(client, instance.id, row.id)
    generic = sources["by_kind"]["generic"]
    explained = await client.post(f"/api/queries/{instance.id}/explain", json={"query": row.query})
    body = explained.json()
    log(
        await _version(admin),
        f"önerilen={sources['recommended']} | generic detay={generic['detail']} | EXPLAIN {explained.status_code} "
        f"kaynak={body.get('source_label')!r} kök düğüm={body.get('plan', {}).get('node_type') if explained.status_code == 200 else body}",
    )
    version_num = int(await admin.fetchval("SELECT current_setting('server_version_num')"))
    assert generic["available"] and generic["detail"]["method"] == "prepare_force_generic_plan"
    assert generic["detail"]["generic_plan_option_available"] is (version_num >= 160000)
    assert explained.status_code == 200 and "$1" in str(body["raw_plan"])


# --- 3.6: EXPLAIN'in iç sorgusu pg_stat_statements'a düşüyor mu ---------------------------


@pytest.mark.parametrize("track", ["all", "top"])
async def test_measure_explain_footprint_in_pg_stat_statements(admin, client, dsn, track):
    """ÖLÇÜM. dbace'in plan almak için gönderdiği ifadeler hangi kayıtları üretiyor ve
    uygulama sorgusunun queryid'sine dokunuyor mu."""
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=True)
    app_queryid = row.queryid
    await admin.execute(f"ALTER ROLE dbace_it_super SET pg_stat_statements.track = '{track}'")
    try:
        results = {}
        for path, payload in (
            ("generic", {"query": row.query}),
            ("sample_analyze", {"query": row.query, "use_sample": True, "sample_id": row.id}),
        ):
            query_cache_key_buster = {"query": payload["query"] + " "} if path == "generic" else {}
            await admin.execute("SELECT pg_stat_statements_reset()")
            response = await client.post(f"/api/queries/{instance.id}/explain", json=payload | query_cache_key_buster)
            assert response.status_code == 200, response.text
            rows = await admin.fetch(
                "SELECT s.queryid::text AS queryid, s.toplevel, s.calls, left(s.query, 90) AS query "
                "FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
                "WHERE r.rolname = 'dbace_it_super' ORDER BY s.toplevel DESC, s.query"
            )
            results[path] = rows
            touches_app = [r for r in rows if r["queryid"] == app_queryid]
            log(
                f"{await _version(admin)} track={track} {path}",
                f"{len(rows)} kayıt; uygulama sorgusunun queryid'sine dokunan: "
                f"{[(r['toplevel'], r['calls'], r['query']) for r in touches_app]}",
            )
            for r in rows:
                log("    kayıt", f"toplevel={r['toplevel']} calls={r['calls']} {r['query']!r}")
        if track == "top":
            assert all(r["toplevel"] for rows in results.values() for r in rows)
    finally:
        await admin.execute("ALTER ROLE dbace_it_super RESET pg_stat_statements.track")



# --- Ölçülen sızıntı: PG 15 EXPLAIN metnindeki değeri pg_stat_statements'ta tutuyor ----------


async def test_dbace_never_stores_the_value_even_where_the_server_keeps_it(admin, client, dsn):
    """ÖLÇÜLDÜ: PG 15 yardımcı ifadelerin sabitlerini pg_stat_statements'ta normalize etmiyor
    (16.15 / 17.11 / 18.6 ediyor). Gerçek değerli örnekle EXPLAIN ANALYZE sonrası:
    - sunucunun kendi görünümü sürüme göre değeri tutuyor ya da tutmuyor (belgeleniyor),
    - dbace'in GERÇEK toplama döngüsü o satırı değer OLMADAN saklıyor (her sürümde),
    - plan kaynakları uyarısı sürüme göre doğru."""
    instance, row, marker, fast, slow = await _prepare_sample(admin, client, dsn, store=True)
    sources = await _options(client, instance.id, row.id)
    await admin.execute("SELECT pg_stat_statements_reset()")
    assert (await _explain_sample(client, instance.id, row)).status_code == 200

    server_texts = [
        r["query"] for r in await admin.fetch(
            "SELECT s.query FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
            "WHERE r.rolname = 'dbace_it_super' AND s.query LIKE '%EXPLAIN (ANALYZE%'"
        )
    ]
    server_keeps_value = any(slow in t for t in server_texts)

    await _collect(instance.id)
    async with SessionLocal() as session:
        stored = (
            await session.execute(
                select(SlowQuerySample.query).where(
                    SlowQuerySample.instance_id == instance.id, SlowQuerySample.query.like("%EXPLAIN (ANALYZE%")
                )
            )
        ).scalars().all()

    version_num = int(await admin.fetchval("SELECT current_setting('server_version_num')"))
    flag = sources["by_kind"]["sample_analyze"]["detail"]["leaves_values_in_server_statistics"]
    log(
        await _version(admin),
        f"sunucu değeri tutuyor={server_keeps_value} | dbace'te saklanan={stored} | uyarı bayrağı={flag}",
    )
    assert server_keeps_value is (version_num < 160000), "ölçülen sürüm davranışı değişmiş"
    assert flag is (version_num < 160000)
    assert stored, "toplama dbace'in EXPLAIN satırını okumalıydı — test bir şey kanıtlamıyor"
    assert all(slow not in text for text in stored), "gerçek değer dbace'in veritabanına yazılmış"
