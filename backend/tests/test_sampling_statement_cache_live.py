"""Örnekleme bağlantısında hazırlanmış ifade önbelleği — GERÇEK PostgreSQL üzerinde (Faz 31 Commit 10a).

1. **Gidiş-dönüş sayısı ölçülüyor:** hedef, sabit gecikme ekleyen gerçek bir TCP vekilinin ARKASINDA. Önbellekli
   örnekleme ≈ 1 RTT, önbelleksiz (eski davranış) ≈ 2 RTT. Süre bir gecikme gösterge değeri değil, protokol
   gidiş-dönüşlerinin doğrudan sonucu.
2. **Gerçek havuzlayıcı:** PgBouncer (işlem modu) arkasında ilk örnekleme hatası önbelleği kalıcı kapatıyor,
   örnekleyici kendiliğinden yeniden bağlanıp örneklemeye devam ediyor (bkz. `DBACE_TEST_PG_POOLER_DSN`).

Kısıtlı izleme rolüyle (`dbace_monitor`, pg_monitor, superuser YOK).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from app.collectors.postgresql import PostgreSQLCollector
from tests.live_pg import LIVE_DSNS, POOLER_DSN, SKIP_REASON, prepare_restricted_database, restricted_target
from tests.net_proxy import Proxy

pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

RTT_MS = 300
_POOLER_ERRORS = (asyncpg.exceptions.DuplicatePreparedStatementError,
                 asyncpg.exceptions.InvalidSQLStatementNameError)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


async def _time_samples(collector: PostgreSQLCollector, count: int = 4) -> list[float]:
    conn = await collector.open_sampling_connection()
    try:
        await collector.sample_active_sessions(conn)  # ilk örnek ifadeyi hazırlar; ölçüme girmiyor
        durations = []
        for _ in range(count):
            started = time.monotonic()
            await collector.sample_active_sessions(conn)
            durations.append(time.monotonic() - started)
        return durations
    finally:
        await conn.raw.close()


async def test_statement_cache_halves_the_round_trips_of_a_sample_on_a_delayed_link():
    dsn = LIVE_DSNS[0]
    await prepare_restricted_database(dsn)
    target = restricted_target(dsn)
    proxy = Proxy(RTT_MS, target.port)
    try:
        target.port = proxy.port
        cached = PostgreSQLCollector(target)
        uncached = PostgreSQLCollector(target)
        uncached._statement_cache_unsafe = True  # eski davranış: önbellek kapalı

        with_cache = await _time_samples(cached)
        without_cache = await _time_samples(uncached)
    finally:
        proxy.close()

    rtt = RTT_MS / 1000
    log("önbellekli örnek süreleri (sn)", [round(d, 3) for d in with_cache])
    log("önbelleksiz örnek süreleri (sn)", [round(d, 3) for d in without_cache])
    assert max(with_cache) < rtt * 1.5, "önbellekli örnek tek gidiş-dönüş (≈ 1 RTT) sürmeli"
    assert min(without_cache) > rtt * 1.8, "önbelleksiz örnek iki gidiş-dönüş (≈ 2 RTT) sürer (negatif kontrol)"
    assert sum(without_cache) / sum(with_cache) > 1.6


async def test_sample_result_is_identical_with_and_without_the_cache():
    """NEGATİF KONTROL: önbellek yalnızca hızı değiştirmeli, örneğin içeriğini değil."""
    dsn = LIVE_DSNS[0]
    await prepare_restricted_database(dsn)
    target = restricted_target(dsn)
    cached, uncached = PostgreSQLCollector(target), PostgreSQLCollector(target)
    uncached._statement_cache_unsafe = True
    a, b = await cached.open_sampling_connection(), await uncached.open_sampling_connection()
    try:
        assert a.capabilities["statement_cache"] is True and b.capabilities["statement_cache"] is False
        first, second = await cached.sample_active_sessions(a), await uncached.sample_active_sessions(b)
        assert first.keys() == second.keys() == {"sessions", "blocked", "has_query_id"}
        # Aynı sorgu ikinci kez (önbellekten) çalışınca da aynı biçim.
        again = await cached.sample_active_sessions(a)
        assert again.keys() == first.keys()
    finally:
        await a.raw.close()
        await b.raw.close()



async def test_behind_a_real_pooler_the_cache_switches_itself_off_and_sampling_continues():
    """GERÇEK PgBouncer (işlem modu, max_prepared_statements=0): iki örnekleyici + bir komşu istemci aynı havuzu paylaşıyor.

    Kendi kurulumundaki bir PgBouncer adresten anlaşılamaz (`resolve_uses_pooler` yalnızca sezgisel), bu yüzden önbellek
    açık başlar; havuzlayıcı hatası olursa kalıcı kapanmalı, örnekleyici kendiliğinden yeniden bağlanıp örneklemeye
    devam etmeli. Havuzun sunucu bağlantısı istemci kapanışlarında yeniden kurulabildiği için çakışma her denemede
    oluşmuyor: senaryo çakışma gözlenene dek (en çok 8 kez) tekrarlanır ve her denemede — çakışma olsun olmasın — örnekleme sürmelidir."""
    from urllib.parse import urlparse

    from app.collectors.base import ConnectionTarget, resolve_uses_pooler
    from app.database import SessionLocal, init_db
    from app.models import Instance
    from app.services import wait_sampling
    from app.services.credentials import encrypt_secret

    assert POOLER_DSN, "DBACE_TEST_PG_POOLER_DSN tanımlı değil — `python scripts/live_pg.py up` yazdırır"
    pg16 = [d for d in LIVE_DSNS if ":55434/" in d]
    assert pg16, "PgBouncer'ın arkasındaki PostgreSQL 16 birincili (55434) DBACE_TEST_PG_DSN'de yok"
    await prepare_restricted_database(pg16[0])  # rol ve veritabanı (PgBouncer bu role bağlanır)
    await init_db()
    url = urlparse(POOLER_DSN)

    async def scenario() -> tuple[list[bool], list[int], list[int]]:
        # Havuzu paylaşan ikinci bir uygulama: ifade adlarını doldurur ve scenario boyunca AÇIK kalır.
        neighbour = await asyncpg.connect(POOLER_DSN, statement_cache_size=100)
        try:
            for index in range(12):
                try:
                    await neighbour.fetch(f"SELECT {index} AS komsu_{index}")
                except _POOLER_ERRORS:
                    pass  # adlar zaten dolu — istenen durum
            wait_sampling.reset_state()
            instances, collectors = [], []
            for index in range(2):
                target = ConnectionTarget(host=url.hostname, port=url.port, database=url.path.lstrip("/"),
                                          username=url.username, password=url.password, options={})
                assert resolve_uses_pooler(target.options, target.host, target.port) is False,                     "adres havuzlayıcı gibi görünmüyor: bu yüzden önbellek açık başlıyor (test bunun için var)"
                collector = PostgreSQLCollector(target)
                instance = Instance(name=f"pooler-{index}-{uuid.uuid4().hex[:4]}", engine="postgresql",
                                    host=target.host, port=target.port, database=target.database,
                                    username=target.username, password=encrypt_secret(target.password), enabled=True)
                async with SessionLocal() as session:  # kapanışta yazım yabancı anahtar ister: gerçek satır
                    session.add(instance)
                    await session.commit()
                    await session.refresh(instance)
                wait_sampling._samplers[instance.id] = wait_sampling._InstanceSampler(
                    instance_id=instance.id, engine=wait_sampling.DatabaseEngine.POSTGRESQL, collector=collector)
                instances.append(instance)
                collectors.append(collector)
            now = datetime.now(UTC)
            for _ in range(12):
                for instance in instances:
                    await wait_sampling._sample_instance(instance, now)
            result = ([c._statement_cache_unsafe for c in collectors],
                      [wait_sampling._buckets[i.id].samples_taken for i in instances],
                      [wait_sampling._samplers[i.id].consecutive_failures for i in instances])
            await wait_sampling.shutdown_sampling()
            return result
        finally:
            await neighbour.close()
            wait_sampling.reset_state()

    history = []
    for attempt in range(1, 9):
        unsafe, taken, failing = await scenario()
        history.append((unsafe, taken))
        assert all(t >= 8 for t in taken), f"deneme {attempt}: örnekleme sürmeli, alınan {taken}"
        assert failing == [0, 0], f"deneme {attempt}: örnekleyiciler kalıcı hata durumunda kalmamalı"
        if any(unsafe):
            break  # çakışma yaşandı, önbellek kapandı ve örnekleme sürdü — gerçek havuzlayıcıda kanıt
    log("denemeler (önbellek kapandı mı, alınan örnek)", history)
    # Not: çakışmanın oluşup oluşmaması PgBouncer'ın iç durumuna (sunucu bağlantısının yeniden kurulması) bağlı;
    # o yüzden burada ZORUNLU DEĞİL. Hatanın kendisi ve toparlanma deterministik olarak
    # `test_a_lost_prepared_statement_switches_the_cache_off_and_sampling_recovers` ile kanıtlanıyor.


async def test_a_lost_prepared_statement_switches_the_cache_off_and_sampling_recovers():
    """DETERMİNİSTİK gerçek hata: örnekleme bağlantısında `DEALLOCATE ALL` — asyncpg önbelleğinde ifadeyi hâlâ
    "hazırlanmış" sanıyor ama sunucuda yok: gerçek PostgreSQL `InvalidSQLStatementNameError` (26000) üretir. Havuzlayıcının
    ardışık ifadeleri başka bir sunucu bağlantısına atlatmasıyla AYNI hata sınıfı. NEGATİF KONTROL: hatanın kendisi
    gerçekten oluşuyor ve bağlantı düşürülüp önbelleksiz yeniden kuruluyor."""
    from app.database import SessionLocal, init_db
    from app.models import Instance
    from app.services import wait_sampling
    from app.services.credentials import encrypt_secret

    dsn = LIVE_DSNS[0]
    await prepare_restricted_database(dsn)
    target = restricted_target(dsn)
    await init_db()
    wait_sampling.reset_state()
    collector = PostgreSQLCollector(target)
    instance = Instance(name=f"lost-stmt-{uuid.uuid4().hex[:6]}", engine="postgresql", host=target.host, port=target.port,
                        database=target.database, username=target.username, password=encrypt_secret(target.password),
                        enabled=True)
    async with SessionLocal() as session:
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
    wait_sampling._samplers[instance.id] = wait_sampling._InstanceSampler(
        instance_id=instance.id, engine=wait_sampling.DatabaseEngine.POSTGRESQL, collector=collector)
    now = datetime.now(UTC)

    await wait_sampling._sample_instance(instance, now)   # bağlanır (önbellekli), ifade hazırlanır
    await wait_sampling._sample_instance(instance, now)   # önbellekten
    sampler = wait_sampling._samplers[instance.id]
    assert sampler.conn.capabilities["statement_cache"] is True
    await sampler.conn.raw.execute("DEALLOCATE ALL")      # sunucu ifadeyi unuttu
    with pytest.raises(asyncpg.exceptions.InvalidSQLStatementNameError):
        await collector.sample_active_sessions(sampler.conn)  # gerçek hata (negatif kontrol: senaryo boş geçmiyor)
    assert collector._statement_cache_unsafe is True

    await wait_sampling._sample_instance(instance, now)   # örnekleyici yolu: hata → düşür → önbelleksiz yeniden bağlan
    await wait_sampling._sample_instance(instance, now)
    reopened = wait_sampling._samplers[instance.id].conn
    log("yeniden bağlantı", {"önbellek": reopened.capabilities["statement_cache"] if reopened else None,
                             "başarısız tur": wait_sampling._samplers[instance.id].consecutive_failures})
    assert reopened is not None and reopened.capabilities["statement_cache"] is False
    assert wait_sampling._samplers[instance.id].consecutive_failures == 0
    assert wait_sampling._buckets[instance.id].samples_taken >= 3
    await wait_sampling.shutdown_sampling()
    wait_sampling.reset_state()
