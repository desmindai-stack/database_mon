"""PostgreSQL örnekleme bağlantısında hazırlanmış ifade önbelleği (Faz 31 Commit 10a).

Ölçüm: hedef bağlantısı `statement_cache_size=0` (PgBouncer/Supabase havuzlayıcı uyumu için bilerek) ile açılıyor;
önbelleksiz her sorgu Parse + Execute = 2 gidiş-dönüş. 1 saniyelik örneklemede uzak bir hedefte bu, ağ gecikmesinin
ikiye katlanması (250 ms RTT'de örnek başına 558 ms). Örnekleme sorgusu her turda AYNI metin olduğundan önbellek ilk
turdan sonra tek gidiş-dönüşe indiriyor.

Ama havuzlayıcı sezgisel tespit (kendi PgBouncer'ı adresten anlaşılamaz), bu yüzden düz "önbelleği aç" GÜVENSİZ:
- havuzlayıcı açıkça bildirilmişse ya da adresten anlaşılıyorsa önbellek KAPALI;
- açık başlayıp havuzlayıcı hatası (`prepared statement ... does not exist / already exists`) görülürse o hedef için
  KALICI kapanır, bağlantı yeniden kurulur, örnekleme sürer (bir tur kaybı, bir kez).

Gerçek PgBouncer kanıtı: test_sampling_statement_cache_live.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from app.collectors.base import ConnectionTarget, SamplingConnection
from app.collectors.postgresql import PostgreSQLCollector


class FakeRaw:
    """asyncpg bağlantısı yerine: davranışı testin belirlediği sahte."""

    def __init__(self, fetch_error: Exception | None = None) -> None:
        self.fetch_error = fetch_error
        self.fetches = 0

    async def execute(self, *args, **kwargs):
        return "SET"

    async def fetch(self, *args, **kwargs):
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return []

    async def close(self):
        pass


def _collector(monkeypatch, *, options=None, host="db.example.com", port=5432, errors=None):
    """`_connect`'in aldığı `statement_cache` değerlerini kaydeden toplayıcı."""
    collector = PostgreSQLCollector(ConnectionTarget(host=host, port=port, database="d", username="u", password="p",
                                                     options=options or {}))
    seen: list[bool] = []
    queue = list(errors or [])

    async def fake_connect(self, *, statement_cache: bool = False):
        seen.append(statement_cache)
        return FakeRaw(queue.pop(0) if queue else None)

    async def fake_version(self, conn):
        return 170000, "17"

    monkeypatch.setattr(PostgreSQLCollector, "_connect", fake_connect)
    monkeypatch.setattr(PostgreSQLCollector, "_detect_version", fake_version)
    return collector, seen


async def test_sampling_connection_opens_with_the_statement_cache_by_default(monkeypatch):
    collector, seen = _collector(monkeypatch)
    conn = await collector.open_sampling_connection()
    assert seen == [True] and conn.capabilities["statement_cache"] is True


async def test_declared_pooler_keeps_the_cache_off(monkeypatch):
    """NEGATİF KONTROL: havuzlayıcı açıkça bildirilmişse önbellek açılmıyor."""
    collector, seen = _collector(monkeypatch, options={"uses_pooler": True})
    conn = await collector.open_sampling_connection()
    assert seen == [False] and conn.capabilities["statement_cache"] is False


async def test_a_prepared_statement_error_disables_the_cache_permanently_for_that_target(monkeypatch):
    error = asyncpg.exceptions.InvalidSQLStatementNameError("prepared statement \"__asyncpg_stmt_1__\" does not exist")
    collector, seen = _collector(monkeypatch, errors=[error])
    first = await collector.open_sampling_connection()
    with pytest.raises(asyncpg.exceptions.InvalidSQLStatementNameError):
        await collector.sample_active_sessions(first)
    assert collector._statement_cache_unsafe is True

    second = await collector.open_sampling_connection()  # örnekleyici bağlantıyı düşürüp yeniden kurar
    assert seen == [True, False] and second.capabilities["statement_cache"] is False
    assert await collector.sample_active_sessions(second) == {"sessions": [], "blocked": 0, "has_query_id": True}


async def test_duplicate_prepared_statement_error_is_treated_the_same(monkeypatch):
    error = asyncpg.exceptions.DuplicatePreparedStatementError("prepared statement already exists")
    collector, _ = _collector(monkeypatch, errors=[error])
    conn = await collector.open_sampling_connection()
    with pytest.raises(asyncpg.exceptions.DuplicatePreparedStatementError):
        await collector.sample_active_sessions(conn)
    assert collector._statement_cache_unsafe is True


async def test_unrelated_errors_do_not_disable_the_cache(monkeypatch):
    """NEGATİF KONTROL: ağ hatası gibi ilgisiz bir hata önbelleği kapatmamalı (yoksa her kopmada 2 RTT'ye düşerdik)."""
    collector, seen = _collector(monkeypatch, errors=[ConnectionResetError("koptu")])
    conn = await collector.open_sampling_connection()
    with pytest.raises(ConnectionResetError):
        await collector.sample_active_sessions(conn)
    assert collector._statement_cache_unsafe is False
    await collector.open_sampling_connection()
    assert seen == [True, True]


async def test_error_on_a_cacheless_connection_changes_nothing(monkeypatch):
    error = asyncpg.exceptions.InvalidSQLStatementNameError("does not exist")
    collector, _ = _collector(monkeypatch, options={"uses_pooler": True}, errors=[error])
    conn = await collector.open_sampling_connection()
    with pytest.raises(asyncpg.exceptions.InvalidSQLStatementNameError):
        await collector.sample_active_sessions(conn)
    assert collector._statement_cache_unsafe is False, "kapatılacak bir önbellek yoktu"


async def test_the_sampler_recovers_by_itself_after_a_pooler_error(monkeypatch):
    """Örnekleyici yolu: hata → bağlantı düşer → önbelleksiz yeniden bağlanır → örnekleme sürer."""
    from app.database import init_db
    from app.models import Instance
    from app.services import wait_sampling
    from app.services.credentials import encrypt_secret

    await init_db()
    wait_sampling.reset_state()
    error = asyncpg.exceptions.InvalidSQLStatementNameError("does not exist")
    collector, seen = _collector(monkeypatch, errors=[error])
    instance = Instance(name="cache-recover", engine="postgresql", host="h", port=5432, database="d", username="u",
                        password=encrypt_secret("x"), enabled=True)
    instance.id = 8801
    wait_sampling._samplers[instance.id] = wait_sampling._InstanceSampler(
        instance_id=instance.id, engine=wait_sampling.DatabaseEngine.POSTGRESQL, collector=collector)

    now = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    await wait_sampling._sample_instance(instance, now)                       # havuzlayıcı hatası
    assert wait_sampling._samplers[instance.id].conn is None
    await wait_sampling._sample_instance(instance, now)                       # önbelleksiz yeniden bağlanma
    await wait_sampling._sample_instance(instance, now)
    assert seen == [True, False]
    assert wait_sampling._buckets[instance.id].samples_taken == 2
    wait_sampling.reset_state()


def test_default_connections_stay_cacheless_only_sampling_opts_in():
    """Toplayıcının diğer bağlantıları (`_connect()` çağrıları) önbelleksiz kalıyor: yalnızca sürekli örnekleme
    bağlantısı isteğe bağlı olarak açıyor."""
    import inspect

    source = inspect.getsource(PostgreSQLCollector)
    assert source.count("_connect(statement_cache=use_cache)") == 1
    assert "statement_cache_size=100 if statement_cache else 0" in source
    assert inspect.signature(PostgreSQLCollector._connect).parameters["statement_cache"].default is False


def test_sampling_connection_type_is_unchanged():
    assert SamplingConnection.__dataclass_fields__.keys() >= {"raw", "capabilities"}


async def test_a_pooler_error_during_connection_setup_reopens_the_connection_without_the_cache(monkeypatch):
    """Havuzlayıcı hatası bağlantı KURULUMUNDA (sürüm tespiti) da çıkabiliyor — gerçek PgBouncer'da ölçüldü."""
    collector, seen = _collector(monkeypatch)
    calls = {"n": 0}

    async def flaky_version(self, conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncpg.exceptions.DuplicatePreparedStatementError("prepared statement already exists")
        return 170000, "17"

    monkeypatch.setattr(PostgreSQLCollector, "_detect_version", flaky_version)
    conn = await collector.open_sampling_connection()
    assert seen == [True, False], "ikinci bağlantı önbelleksiz kurulmalı"
    assert conn.capabilities["statement_cache"] is False and collector._statement_cache_unsafe is True


async def test_setup_error_without_the_cache_is_not_retried_forever(monkeypatch):
    """NEGATİF KONTROL: önbellek zaten kapalıyken aynı hata tekrar denenmeden yukarı çıkıyor."""
    collector, seen = _collector(monkeypatch, options={"uses_pooler": True})

    async def always_fails(self, conn):
        raise asyncpg.exceptions.DuplicatePreparedStatementError("prepared statement already exists")

    monkeypatch.setattr(PostgreSQLCollector, "_detect_version", always_fails)
    with pytest.raises(asyncpg.exceptions.DuplicatePreparedStatementError):
        await collector.open_sampling_connection()
    assert seen == [False]


async def test_a_failed_setup_closes_the_half_opened_connection(monkeypatch):
    collector, _ = _collector(monkeypatch)
    raws: list[FakeRaw] = []
    real_connect = PostgreSQLCollector._connect

    async def tracking_connect(self, *, statement_cache: bool = False):
        raw = await real_connect(self, statement_cache=statement_cache)
        raws.append(raw)
        return raw

    closed: list[bool] = []

    async def close(self):
        closed.append(True)

    monkeypatch.setattr(PostgreSQLCollector, "_connect", tracking_connect)
    monkeypatch.setattr(FakeRaw, "close", close)

    async def broken_version(self, conn):
        raise ConnectionResetError("koptu")

    monkeypatch.setattr(PostgreSQLCollector, "_detect_version", broken_version)
    with pytest.raises(ConnectionResetError):
        await collector.open_sampling_connection()
    assert closed == [True], "yarım kalan bağlantı kapatılmalı (sızmamalı)"
