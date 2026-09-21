"""Bekleme örnekleyicisi — GERÇEK sunucularda ölçeklenme, aralık ve bloklama yolu (Faz 31 Commit 10a).

Hepsi izleme kimlikleriyle KISITLI: PostgreSQL'de paketin rol SQL'iyle kurulan `dbace_monitor` (pg_monitor,
superuser YOK), SQL Server'da paketin login SQL'iyle kurulan `dbace_monitor`. Yük ve kilitler yönetici kimlikleriyle
üretiliyor (test düzeneği); ÖLÇÜM her zaman kısıtlı kimlikle.

Kanıtlananlar:
1. 20 instance (3 PostgreSQL birincil + 3 replika + SQL Server; aynı sunucuya birden çok kayıt) 1 saniyelik aralığı
   tutuyor, hiçbir tur atlanmıyor.
2. Ağda yavaşlayan TEK bir hedef (gerçek TCP vekili, 5 sn'de bir 2,5 sn ağ aksaması) yalnızca kendi örneklemesini geciktiriyor;
   diğerleri aralığı tutuyor. NEGATİF KONTROL: eski tur biçimi (`wait=True`, hepsini bekleyen) aynı durumda HERKESİN
   örneklemesini yarıya indiriyor — düzeltmenin gerçekten bir şeyi düzelttiğini gösterir.
3. Aynı yavaş hedef ekranda "örnekleme aralığı tutturulamadı (ölçülen: X ms)" olarak çıkıyor (HTTP yolu).
4. Bloklama geçmişi, ağacı yalnızca kilit bekleyen oturum varken okuyor; gerçek bir kilit çatışması iki motorda da
   olay olarak kaydediliyor (örnekleme görevi → yazım işi → geçmiş) ve serbest bırakılınca kapanıyor.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import BlockingEpisode, Instance
from app.services import blocking_history, wait_sampling
from app.services.credentials import encrypt_secret
from tests.live_mssql import (
    APP_DATABASE,
    MONITOR_LOGIN,
    MONITOR_PASSWORD,
    MSSQL_TARGETS,
    SA_PASSWORD,
    prepare_monitor_login,
    standalone_target,
)
from tests.net_proxy import Proxy
from tests.live_pg import (
    LIVE_DSNS,
    RESTRICTED_DATABASE,
    SKIP_REASON,
    prepare_restricted_database,
    restricted_target,
    with_database,
)

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

INTERVAL = 1.0


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    wait_sampling.reset_state()
    blocking_history.reset_state()
    yield
    # Bellek instance'larının (meta veritabanında satırı yok) kovaları yazılmaz: yabancı anahtar hatası verirdi.
    # Veritabanı destekli testler kendi kovalarını açıkça yazıyor.
    wait_sampling._buckets.clear()
    wait_sampling._pending_flush.clear()
    await wait_sampling.shutdown_sampling()
    wait_sampling.reset_state()
    blocking_history.reset_state()


def _memory_instance(index: int, engine: str, target, port: int | None = None) -> Instance:
    """Meta veritabanına yazılmayan, yalnızca bağlantı bilgisi taşıyan instance (tur testleri için)."""
    row = Instance(
        name=f"live-{index:02d}-{uuid.uuid4().hex[:4]}", engine=engine, host=target.host, port=port or target.port,
        database=target.database, username=target.username, password=encrypt_secret(target.password),
        options=target.options or None, enabled=True,
    )
    row.id = 9000 + index
    return row


async def _restricted_targets():
    """(ad, motor, hedef) listesi — hepsi kısıtlı kimlik."""
    targets = []
    for index, dsn in enumerate(LIVE_DSNS):
        await prepare_restricted_database(dsn)
        targets.append((f"pg{index}", "postgresql", restricted_target(dsn)))
    if "standalone" in MSSQL_TARGETS:
        await asyncio.to_thread(prepare_monitor_login)
        targets.append(("mssql", "sqlserver", standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, APP_DATABASE)))
    return targets


@contextlib.contextmanager
def _pin_instances(instances: list[Instance]):
    """Tur, meta veritabanından okumak yerine bu instance'ları kullansın; çıkışta özgün işlevi geri koyar."""
    original = wait_sampling._instances_for_tick

    async def fixed(now, *, refresh):
        return list(instances)

    wait_sampling._instances_for_tick = fixed
    try:
        yield
    finally:
        wait_sampling._instances_for_tick = original


async def _drive(instances: list[Instance], seconds: int, *, wait: bool = False) -> dict[int, int]:
    """Gerçek zamanlayıcı ritmi: her saniye başında bir tur. Örnek sayısı instance başına döner."""
    wait_sampling.reset_state()
    with _pin_instances(instances):
        started = time.monotonic()
        tick = 0
        # SÜREYE bağlı döngü: eski (bekleyen) biçimde bir tur uzarsa sonraki tur gecikir ve toplam tur sayısı düşer —
        # tur sayısına bağlı olsaydı gecikme hiç görünmezdi.
        while time.monotonic() - started < seconds:
            await wait_sampling.sampling_tick(wait=wait, flush=False)
            # APScheduler `coalesce=True` gibi: kaçırılan turlar TELAFİ EDİLMEZ, sonraki sınıra atlanır.
            now = time.monotonic()
            tick = max(tick + 1, int((now - started) / INTERVAL) + 1)
            await asyncio.sleep(max(0.0, started + tick * INTERVAL - now))
        # Son turların görevleri bitsin.
        pending = [s.in_flight for s in wait_sampling._samplers.values() if s.in_flight and not s.in_flight.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
    return {i.id: _taken(i.id)[0] for i in instances}


def _taken(instance_id: int) -> tuple[int, int | None]:
    """(alınan örnek, en uzun boşluk ms) — dakika kenarını geçen koşuda kapanmış kovalar da sayılır."""
    buckets = [b for i, b in wait_sampling._pending_flush if i == instance_id]
    if instance_id in wait_sampling._buckets:
        buckets.append(wait_sampling._buckets[instance_id])
    gaps = [b.max_gap_ms for b in buckets if b.max_gap_ms is not None]
    return sum(b.samples_taken for b in buckets), (max(gaps) if gaps else None)


async def test_twenty_instances_on_real_servers_hold_the_interval_with_restricted_logins():
    targets = await _restricted_targets()
    # Kısıtlı kimliğin gerçekten superuser olmadığını kanıtla (temel kısıt).
    probe = await asyncpg.connect(with_database(LIVE_DSNS[0], RESTRICTED_DATABASE), user=targets[0][2].username,
                                  password=targets[0][2].password, statement_cache_size=0)
    try:
        assert await probe.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user") is False
    finally:
        await probe.close()

    instances = [_memory_instance(i, engine, target) for i, (_, engine, target) in
                 enumerate((targets * 20)[:20])]
    seconds = 20
    counts = await _drive(instances, seconds)
    per_engine: dict[str, list[int]] = {}
    for instance in instances:
        per_engine.setdefault(instance.engine, []).append(counts[instance.id])

    log("instance / farklı sunucu", f"{len(instances)} / {len(targets)}")
    log("örnek sayısı (beklenen ~%d)" % seconds, per_engine)
    log("atlanan tur / gerçek aralık", {"atlanan": wait_sampling._stats.missed_slots,
                                        "önceki örnek sürerken": wait_sampling._stats.skipped_in_flight,
                                        "ort ms": round(wait_sampling._stats.gap_sum / max(wait_sampling._stats.gap_count, 1) * 1000),
                                        "en uzun boşluk ms": round(wait_sampling._stats.gap_max * 1000)})
    for engine, values in per_engine.items():
        assert min(values) >= seconds * 0.9, f"{engine}: her instance en az %90 örneklenmeli, gelen {values}"
    assert wait_sampling._stats.skipped_in_flight == 0
    cadence = wait_sampling.current_cadence()
    assert not cadence.missed, cadence.message
    assert wait_sampling.sampling_status()["instances_failing"] == 0


async def test_one_slow_target_delays_only_itself_and_the_legacy_tick_shape_delays_everyone():
    targets = await _restricted_targets()
    pg_name, pg_engine, pg_target = targets[0]
    proxy = Proxy(0, pg_target.port, stall_ms=2500, stall_every_s=5)  # 5 sn'de bir 2,5 sn aksama
    try:
        slow = _memory_instance(0, pg_engine, pg_target, port=proxy.port)
        fast = [_memory_instance(i + 1, engine, target) for i, (_, engine, target) in enumerate(targets[1:] * 3)][:5]
        seconds = 24

        counts = await _drive([slow, *fast], seconds, wait=False)
        fast_counts = [counts[i.id] for i in fast]
        log("yeni tur: yavaş / hızlılar", (counts[slow.id], fast_counts))
        log("yavaş instance atlanan tur", wait_sampling._samplers[slow.id].skipped_in_flight)
        slow_gap = _taken(slow.id)[1]
        assert min(fast_counts) >= seconds * 0.9, "yavaş bir hedef diğerlerinin örneklemesini geciktirmemeli"
        assert counts[slow.id] <= seconds * 0.85, "aksayan instance kendi aralığını tutturamamalı"
        assert wait_sampling._samplers[slow.id].skipped_in_flight > 0, "atlanan turlar sayılmalı"
        assert slow_gap is not None and slow_gap >= 2000

        # NEGATİF KONTROL: eski tur biçimi (hepsini bekleyen) aynı durumda hızlı instance'ları da yavaşlatıyor.
        legacy = await _drive([slow, *fast], seconds, wait=True)
        legacy_fast = [legacy[i.id] for i in fast]
        log("eski tur biçimi: hızlılar", legacy_fast)
        assert max(legacy_fast) <= min(fast_counts) - 3, "eski biçimde hızlılar da aksayan hedefe bağlı kalmalıydı"
    finally:
        proxy.close()


async def test_screen_reports_the_missed_interval_measured_on_a_real_slow_target():
    """HTTP yolu: gerçek yavaş hedef → örnekleyici → yazım işi → veritabanı yükü ucu → uyarı cümlesi."""
    from tests.auth_helper import authed_client

    targets = await _restricted_targets()
    _, engine, target = targets[0]
    proxy = Proxy(0, target.port, stall_ms=2500, stall_every_s=5)
    try:
        async with SessionLocal() as session:
            row = Instance(name=f"cad-live-{uuid.uuid4().hex[:6]}", engine=engine, host=target.host, port=proxy.port,
                           database=target.database, username=target.username,
                           password=encrypt_secret(target.password), enabled=True)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        # İki dakikanın kenarından geçmek için: dakikanın 5. saniyesinden sonra başla, 70 sn koştur.
        while datetime.now(UTC).second > 50:
            await asyncio.sleep(1)
        wait_sampling.reset_state()
        with _pin_instances([row]):
            started = time.monotonic()
            for tick in range(70):
                await wait_sampling.sampling_tick(wait=False, flush=False)
                await wait_sampling.flush_tick(refresh_instances=False)
                await asyncio.sleep(max(0.0, started + (tick + 1) - time.monotonic()))
        async with SessionLocal() as session:
            await wait_sampling.flush_all_buckets(session)
            await session.commit()

        async with await authed_client() as client:
            response = await client.get(f"/api/instances/{row.id}/database-load?hours=1")
        assert response.status_code == 200, response.text
        body = response.json()
        log("veri", body.get("unavailable_reason") or f"{body['samples_taken']} örnek")
        log("cadence", body["cadence"])
        assert body["cadence"]["missed"] is True
        assert body["cadence"]["measured_interval_ms"] >= 1250
        assert "Örnekleme aralığı tutturulamadı (ölçülen:" in body["cadence"]["message"]
    finally:
        proxy.close()


# --- Bloklama: yalnızca gerektiğinde okunur, gerçek çatışma olay olarak kaydedilir --------------


async def _watch_blocking(instance: Instance, collector_calls: list[float], hold_seconds: float, blocker):
    """Kilit çatışması sırasında ve öncesinde gerçek örnekleme + yazım işini koşturur."""
    real = wait_sampling.BLOCKING_CHECK_INTERVAL_SECONDS
    wait_sampling.BLOCKING_CHECK_INTERVAL_SECONDS = 2.0  # testi kısaltmak için (üretimde 10 sn)
    try:
        with _pin_instances([instance]):
            sampler = await wait_sampling._ensure_sampler(instance)
            original = sampler.collector.collect_blocking

            async def counting(*args, **kwargs):
                collector_calls.append(time.monotonic())
                return await original(*args, **kwargs)

            sampler.collector.collect_blocking = counting  # type: ignore[method-assign]

            started = time.monotonic()
            release_at = None
            for tick in range(int(hold_seconds) + 16):
                if tick == 4:
                    blocker["start"]()
                    release_at = time.monotonic() + hold_seconds
                if release_at is not None and time.monotonic() >= release_at and not blocker.get("released"):
                    blocker["release"]()
                    blocker["released"] = True
                await wait_sampling.sampling_tick(wait=False, flush=False)
                await wait_sampling.flush_tick(refresh_instances=False)
                await asyncio.sleep(max(0.0, started + (tick + 1) - time.monotonic()))
            return started
    finally:
        wait_sampling.BLOCKING_CHECK_INTERVAL_SECONDS = real


async def _episodes(instance_id: int) -> list[BlockingEpisode]:
    async with SessionLocal() as session:
        return list((await session.execute(select(BlockingEpisode).where(
            BlockingEpisode.instance_id == instance_id))).scalars().all())


async def _register_row(engine: str, target) -> Instance:
    async with SessionLocal() as session:
        row = Instance(name=f"blk-{uuid.uuid4().hex[:6]}", engine=engine, host=target.host, port=target.port,
                       database=target.database, username=target.username, password=encrypt_secret(target.password),
                       options=target.options or None, enabled=True)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_postgres_blocking_is_read_only_when_needed_and_a_real_conflict_becomes_an_episode():
    await prepare_restricted_database(LIVE_DSNS[0])
    target = restricted_target(LIVE_DSNS[0])
    instance = await _register_row("postgresql", target)
    admin_dsn = with_database(LIVE_DSNS[0], RESTRICTED_DATABASE)
    holder = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    waiter = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    state: dict = {}
    loop = asyncio.get_running_loop()

    def start() -> None:
        async def go() -> None:
            state["tx"] = holder.transaction()
            await state["tx"].start()
            await holder.execute("UPDATE deadlock_probe SET v = v + 1 WHERE id = 1")
            state["waiting"] = asyncio.create_task(waiter.execute("UPDATE deadlock_probe SET v = v + 1 WHERE id = 1"))
        state["starter"] = loop.create_task(go())

    def release() -> None:
        async def go() -> None:
            await state["tx"].commit()
            await state["waiting"]
        state["releaser"] = loop.create_task(go())

    calls: list[float] = []
    try:
        started = await _watch_blocking(instance, calls, 10.0, {"start": start, "release": release})
    finally:
        await asyncio.gather(*(state[k] for k in ("starter", "releaser") if k in state), return_exceptions=True)
        await holder.close()
        await waiter.close()

    before_block = [c for c in calls if c - started < 4]
    log("bloklama sorgusu sayısı (öncesi / toplam)", (len(before_block), len(calls)))
    assert before_block == [], "kilit bekleyen oturum yokken hedefe bloklama sorgusu gitmemeli"
    assert calls, "gerçek kilit çatışması sırasında ağaç okunmalı"
    episodes = await _episodes(instance.id)
    log("olay", [(e.root_pid, e.max_blocked_sessions, round(e.duration_seconds or 0, 1), e.ended_at is not None)
                 for e in episodes])
    assert episodes and episodes[0].max_blocked_sessions >= 1
    assert episodes[0].ended_at is not None, "serbest bırakılınca olay kapanmalı (açık olay varken kontrol sürdü)"


async def test_sqlserver_blocking_is_read_only_when_needed_and_a_real_conflict_becomes_an_episode():
    if "standalone" not in MSSQL_TARGETS:
        pytest.skip("SQL Server hedefi tanımlı değil")
    import pyodbc

    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    await asyncio.to_thread(prepare_monitor_login)
    target = standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, APP_DATABASE)
    instance = await _register_row("sqlserver", target)
    admin = build_odbc_connection_string(standalone_target("sa", SA_PASSWORD, APP_DATABASE))
    holder = await asyncio.to_thread(pyodbc.connect, admin, autocommit=False)
    waiter = await asyncio.to_thread(pyodbc.connect, admin, autocommit=True)
    state: dict = {}

    def start() -> None:
        holder.cursor().execute("UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = 1")
        import threading
        state["thread"] = threading.Thread(
            target=lambda: waiter.cursor().execute("UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = 1"))
        state["thread"].start()

    def release() -> None:
        holder.commit()

    calls: list[float] = []
    try:
        started = await _watch_blocking(instance, calls, 10.0, {"start": start, "release": release})
    finally:
        if "thread" in state:
            state["thread"].join(timeout=30)
        holder.close()
        waiter.close()

    before_block = [c for c in calls if c - started < 4]
    log("bloklama sorgusu sayısı (öncesi / toplam)", (len(before_block), len(calls)))
    assert before_block == [], "kilit bekleyen oturum yokken hedefe bloklama sorgusu gitmemeli"
    assert calls, "gerçek kilit çatışması sırasında ağaç okunmalı"
    episodes = await _episodes(instance.id)
    log("olay", [(e.root_pid, e.max_blocked_sessions, round(e.duration_seconds or 0, 1), e.ended_at is not None)
                 for e in episodes])
    assert episodes and episodes[0].max_blocked_sessions >= 1
    assert episodes[0].ended_at is not None
