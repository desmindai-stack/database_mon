"""Yardımcı ifade değerleri dbace'in veritabanına GERÇEK yollardan sızmıyor (Faz 31 Commit 5).

Senaryo gerçek sunucuda, gerçek bileşenlerle:

- uygulama (süper kullanıcı, `track_utility=on`) SET, ALTER ROLE PASSWORD, CREATE ROLE PASSWORD ve
  PASSWORD içeren yavaş bir DO bloğu çalıştırıyor — hepsi bir SENTINEL değer taşıyor;
- **toplayıcı** (`collection.collect_instance`, pg_monitor rolü) pg_stat_statements'ı okuyor;
- **örnekleyici** (`wait_sampling._sample_instance` + `flush_all_buckets`) yavaş DO bloğu sürerken
  pg_stat_activity'yi okuyor — gerçek değerli örnek saklama ayarı AÇIK (en kötü durum);
- **bloklama geçmişi** (`wait_sampling._check_blocking`) kök engelleyicinin son ifadesi değer
  taşıyan bir SET iken olayı yazıyor;
- **deadlock**: iki DO bloğu gerçek deadlock üretiyor; `plan_capture.capture_plans_for_instance`
  sunucunun GERÇEK log satırlarını işliyor. Tek ikame: host-agent'ın HTTP isteği — agent journald
  okuyor, konteynerde çalışamıyor; yerine aynı satırlar `pg_read_file` ile sunucudan okunuyor.

Sonra dbace veritabanındaki BÜTÜN tabloların BÜTÜN metin kolonları sentinel için taranıyor (elle
kolon listesi yok). Negatif kontrol: arındırıcılar etkisizleştirilince aynı senaryo sentinel'i
slow_query_samples, blocking_episodes, deadlock_events ve wait_query_signatures'ta buluyor.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import String, Text, text

from app.database import SessionLocal, init_db
from app.models import Base, Instance
from app.services import blocking_history, collection as collection_module, wait_sampling
from app.services import plan_capture, query_text_privacy
from app.services.analysis_settings import set_analysis_settings
from app.services.credentials import encrypt_secret
from app.services.query_text_privacy import PASSWORD_REDACTED_NOTE
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
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS privacy_deadlock_probe (id int PRIMARY KEY, n int);"
        "INSERT INTO privacy_deadlock_probe VALUES (1, 0), (2, 0) ON CONFLICT DO NOTHING"
    )
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _clean_state():
    yield
    for sampler in list(wait_sampling._samplers.values()):
        await wait_sampling._drop_connection(sampler)
    wait_sampling.reset_state()
    blocking_history.reset_state()
    async with SessionLocal() as session:
        await set_analysis_settings(session, store_real_query_samples=False)


async def _instance(dsn: str) -> int:
    t = target_for(dsn, "monitor")
    await init_db()
    async with SessionLocal() as session:
        await set_analysis_settings(session, store_real_query_samples=True)
        instance = Instance(
            name=f"privacy-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
            database=t.database, username=t.username, password=encrypt_secret(ROLE_PASSWORD), enabled=True,
            options={"agent_url": "http://host-agent.invalid"},
        )
        session.add(instance)
        await session.commit()
        return instance.id


async def _collect(instance_id: int) -> None:
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()
    await asyncio.sleep(1.1)


async def _run_scenario(dsn: str, admin, instance_id: int, secret: str, monkeypatch) -> None:
    role = f"privacy_it_{secret[-6:]}"
    await admin.execute("SELECT pg_stat_statements_reset()")
    await _collect(instance_id)

    app = await asyncpg.connect(dsn, statement_cache_size=0)
    blocker = await asyncpg.connect(dsn, statement_cache_size=0)
    victim = await asyncpg.connect(dsn, statement_cache_size=0)
    s1 = await asyncpg.connect(dsn, statement_cache_size=0)
    s2 = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await app.execute("SET pg_stat_statements.track_utility = on")
        await app.execute(f"SET privacy_it.token = '{secret}_set'")
        await app.execute(f"CREATE ROLE {role} PASSWORD '{secret}_create'")
        await app.execute(f"ALTER ROLE {role} PASSWORD '{secret}_alter'")

        async with SessionLocal() as session:
            instance = await session.get(Instance, instance_id)
        done = asyncio.Event()

        async def sampler_loop():
            while not done.is_set():
                await wait_sampling._sample_instance(instance, datetime.now(UTC))
                await asyncio.sleep(0.15)

        task = asyncio.create_task(sampler_loop())
        try:
            # Yavaş (listeye kesin giriyor, örnekleyici görüyor) ve PASSWORD'süz: DO gövdesi arındırılmalı.
            await app.execute(f"DO $$ BEGIN PERFORM '{secret}_do', pg_sleep(1.5); END $$")
            # PASSWORD içeren yavaş DO bloğu: metin hiç saklanmamalı.
            await app.execute(
                f"DO $$ BEGIN PERFORM pg_sleep(0.6); "
                f"EXECUTE 'ALTER ROLE {role} PASSWORD ' || quote_literal('{secret}_dopw'); END $$"
            )
        finally:
            done.set()
            await task

        # Bloklama: kök engelleyicinin son ifadesi değer taşıyan SET.
        await blocker.execute("BEGIN")
        await blocker.execute("LOCK TABLE privacy_deadlock_probe IN ACCESS EXCLUSIVE MODE")
        await blocker.execute(f"SET privacy_it.token = '{secret}_blocker'")
        waiting = asyncio.create_task(victim.fetchval("SELECT count(*) FROM privacy_deadlock_probe"))
        for _ in range(7):
            await asyncio.sleep(1)
            wait_sampling._samplers[instance_id].last_blocking_check = None
            async with SessionLocal() as session:
                await wait_sampling._check_blocking(session, instance, datetime.now(UTC))
                await session.commit()
        await blocker.execute("ROLLBACK")
        await waiting
        async with SessionLocal() as session:
            await wait_sampling.flush_all_buckets(session)
            await blocking_history.close_all(session)
            await session.commit()

        # Deadlock — gerçek sunucu log'u.
        logfile = await admin.fetchval("SELECT pg_current_logfile()")
        assert logfile, "logging_collector kapalı: `python scripts/live_pg.py up` konteyneri günceller"
        offset = await admin.fetchval("SELECT (pg_stat_file($1)).size", logfile)
        await s1.execute("BEGIN")
        await s1.execute("UPDATE privacy_deadlock_probe SET n = n + 1 WHERE id = 1")
        await s2.execute("BEGIN")
        await s2.execute("UPDATE privacy_deadlock_probe SET n = n + 1 WHERE id = 2")
        first = asyncio.create_task(s1.execute(
            f"DO $$ BEGIN PERFORM '{secret}_dl1'; UPDATE privacy_deadlock_probe SET n = n + 1 WHERE id = 2; END $$"))
        await asyncio.sleep(0.5)
        errors = []
        for runner in (
            lambda: s2.execute(
                f"DO $$ BEGIN PERFORM '{secret}_dl2'; UPDATE privacy_deadlock_probe SET n = n + 1 WHERE id = 1; END $$"),
            lambda: first,
        ):
            try:
                await runner()
            except asyncpg.exceptions.DeadlockDetectedError as exc:
                errors.append(exc)
        assert len(errors) == 1, "deadlock üretilemedi"
        for conn in (s1, s2):
            await conn.execute("ROLLBACK")
        await asyncio.sleep(0.5)
        server_log = await admin.fetchval(
            "SELECT pg_read_file($1, $2, (pg_stat_file($1)).size - $2)", logfile, offset
        )
        assert f"{secret}_dl" in server_log, "sunucu log'unda deadlock ayrıntısı yok"

        async def agent_returns_server_log(options, service, lines=100, timeout=5.0):
            return {"lines": server_log.splitlines()}

        monkeypatch.setattr(plan_capture, "fetch_agent_logs", agent_returns_server_log)
        async with SessionLocal() as session:
            outcome = await plan_capture.capture_plans_for_instance(session, instance)
            await session.commit()
        assert outcome["deadlocks"] == 1, outcome

        await _collect(instance_id)
    finally:
        for conn in (app, blocker, victim, s1, s2):
            await conn.close()
        await admin.execute(f"DROP ROLE IF EXISTS {role}")


async def _sentinel_hits(secret: str) -> dict[str, int]:
    """dbace veritabanının bütün metin kolonları — modellerden, elle liste yok."""
    hits: dict[str, int] = {}
    async with SessionLocal() as session:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if isinstance(column.type, (String, Text)):
                    count = (await session.execute(
                        text(f'SELECT count(*) FROM {table.name} WHERE "{column.name}" LIKE :p'), {"p": f"%{secret}%"}
                    )).scalar()
                    if count:
                        hits[f"{table.name}.{column.name}"] = count
    return hits


async def _rows(sql: str, instance_id: int):
    async with SessionLocal() as session:
        return (await session.execute(text(sql), {"i": instance_id})).all()


async def test_utility_values_never_reach_dbaces_database(admin, dsn, monkeypatch):
    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    secret = f"gizli{uuid.uuid4().hex[:10]}"
    instance_id = await _instance(dsn)
    await _run_scenario(dsn, admin, instance_id, secret, monkeypatch)

    hits = await _sentinel_hits(secret)
    slow = await _rows("SELECT query FROM slow_query_samples WHERE instance_id = :i AND "
                       "(query LIKE 'DO %' OR query LIKE 'ALTER ROLE%' OR query LIKE 'CREATE ROLE%' OR query LIKE 'SET %')",
                       instance_id)
    episodes = await _rows("SELECT root_query FROM blocking_episodes WHERE instance_id = :i", instance_id)
    deadlocks = await _rows("SELECT victim_query, winner_query FROM deadlock_events WHERE instance_id = :i", instance_id)
    log(f"PG {version} dbace'te sentinel", hits or "0 kolon")
    log("slow_query_samples yardımcı ifadeler", sorted({r[0] for r in slow}))
    log("blocking_episodes.root_query", [r[0] for r in episodes])
    log("deadlock_events", [tuple(r) for r in deadlocks])

    assert hits == {}
    # Boş kanıt değil: satırlar GERÇEKTEN yazıldı, arındırılmış hâlleriyle.
    texts = {r[0] for r in slow}
    assert f"DO /* {PASSWORD_REDACTED_NOTE} */" in texts
    assert "DO $$ BEGIN PERFORM $1, pg_sleep($2); END $$" in texts
    assert [r[0] for r in episodes] == ["SET privacy_it.token = $1"]
    assert len(deadlocks) == 1 and all(q.startswith("DO $$ BEGIN PERFORM $1; UPDATE") for q in deadlocks[0])


async def test_negative_control_without_the_sanitizer_the_same_scenario_leaks(admin, dsn, monkeypatch):
    identity = lambda sql, *, keep_values: sql  # noqa: E731
    for module in (query_text_privacy, collection_module, blocking_history, plan_capture):
        if hasattr(module, "sanitize_stored_query"):
            monkeypatch.setattr(module, "sanitize_stored_query", identity)
    from app.services import index_advice_watch

    monkeypatch.setattr(index_advice_watch, "sanitize_stored_query", identity)
    monkeypatch.setattr(plan_capture, "sanitize_deadlock_detail", lambda detail, *, source: detail)
    monkeypatch.setattr(query_text_privacy, "is_utility_statement", lambda sql: False)

    secret = f"gizli{uuid.uuid4().hex[:10]}"
    instance_id = await _instance(dsn)
    await _run_scenario(dsn, admin, instance_id, secret, monkeypatch)
    hits = await _sentinel_hits(secret)
    log("arındırıcı etkisizken sentinel", hits)
    for column in ("slow_query_samples.query", "blocking_episodes.root_query", "deadlock_events.victim_query",
                   "deadlock_events.raw_detail", "wait_query_signatures.sample_query_text"):
        assert hits.get(column), f"negatif kontrol {column} sızıntısını göremedi: {hits}"
