""""Yakalanan plan yok" durumunun ayrımı — gerçek sunucuda, gerçek çağrı yoluyla (Faz 31 Commit 5).

Canlıda captured_plans = 0 idi ve arayüz agent tanımlıyken HER durumda "henüz yakalanmış plan
yok" diyordu: hedefte auto_explain kapalı, log okunamıyor ya da iş hiç çalışmıyor da aynı
metni üretiyordu. Artık ayrım GET /captured-plans ve GET /plan-sources yanıtında `unavailable_kind`.

- **not_measured (iş çalışmadı)**: agent tanımlı, yakalama turu hiç koşmadı.
- **disabled_on_target**: gerçek toplayıcı hedefin shared_preload_libraries'ini okudu —
  test konteynerinde auto_explain YOK (ölçülüyor, varsayılmıyor).
- **not_measured (log okunamıyor)**: auto_explain durumu okunamayan rol (pg_read_all_settings
  yok) + agent adresine GERÇEK HTTP isteği bağlantı hatasıyla düşüyor (gerçek tur fonksiyonu).
- **no_plans_yet**: aynı rol, log okunabiliyor. Tek ikame: agent'ın HTTP yanıtı yerine sunucunun
  kendi log satırları (`pg_read_file`) — agent journald okuyor, konteynerde çalışamıyor.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample
from app.services import collection as collection_module
from app.services import plan_capture
from app.services.credentials import encrypt_secret
from tests.live_pg import LIVE_DSNS, ROLE_PASSWORD, SKIP_REASON, dsn_id, prepare_live_database, target_for

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

UNREACHABLE_AGENT = "http://127.0.0.1:9"


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


async def _instance(dsn: str, role: str, agent_url: str) -> int:
    t = target_for(dsn, role)
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"capture-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
            database=t.database, username=t.username, password=encrypt_secret(ROLE_PASSWORD), enabled=True,
            options={"agent_url": agent_url},
        )
        session.add(instance)
        await session.flush()
        session.add(SlowQuerySample(instance_id=instance.id, queryid="1", query="SELECT 1", calls=1,
                                    total_time_ms=1, mean_time_ms=1, rows=1))
        await session.commit()
        return instance.id


async def _collect(instance_id: int) -> None:
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()


async def _unavailable(instance_id: int) -> tuple[str, str, str]:
    from tests.auth_helper import authed_client

    async with SessionLocal() as session:
        from sqlalchemy import select

        sample_id = (await session.execute(
            select(SlowQuerySample.id).where(SlowQuerySample.instance_id == instance_id).limit(1)
        )).scalar_one()
    async with await authed_client() as client:
        listing = (await client.get(f"/api/queries/{instance_id}/captured-plans")).json()
        sources = (await client.get(f"/api/queries/{instance_id}/plan-sources", params={"sample_id": sample_id})).json()
    captured = next(o for o in sources["options"] if o["kind"] == "captured")
    assert captured["detail"]["unavailable_kind"] == listing["unavailable_kind"], "iki uç aynı sonucu vermeli"
    assert captured["reason"] == listing["unavailable_reason"]
    return listing["unavailable_kind"], listing["unavailable_reason"], captured["reason"]


async def test_capture_job_never_ran_is_not_measured(admin, dsn):
    kind, reason, _ = await _unavailable(await _instance(dsn, "super", UNREACHABLE_AGENT))
    log("iş çalışmadı", (kind, reason))
    assert kind == "not_measured" and "henüz hiç okumadı" in reason


async def test_auto_explain_absent_on_target_is_measured_by_the_real_collector(admin, dsn):
    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    preload = await admin.fetchval("SHOW shared_preload_libraries")
    instance_id = await _instance(dsn, "monitor", UNREACHABLE_AGENT)
    await _collect(instance_id)
    kind, reason, _ = await _unavailable(instance_id)
    log(f"PG {version} shared_preload_libraries={preload!r}", (kind, reason))
    assert "auto_explain" not in preload
    assert kind == "disabled_on_target" and "KAPALI" in reason


async def test_unreadable_log_is_not_measured_and_readable_log_is_no_plans_yet(admin, dsn, monkeypatch):
    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    # Log dosyası sunucu genelinde paylaşılıyor: başka testlerin auto_explain planları da orada (Faz 31
    # Commit 6'da tam pakette yakalandı). Yalnızca bu testin başladığı andan sonraki satırlar.
    logfile = await admin.fetchval("SELECT pg_current_logfile()")
    offset = await admin.fetchval("SELECT (pg_stat_file($1)).size", logfile)
    instance_id = await _instance(dsn, "app", UNREACHABLE_AGENT)
    await _collect(instance_id)
    async with SessionLocal() as session:
        loaded = (await session.get(Instance, instance_id)).auto_explain_loaded
    assert loaded is None, "pg_read_all_settings'siz rol preload'u okuyamamalı (ölçüldü)"

    totals = await plan_capture.capture_plans_tick()
    kind, reason, _ = await _unavailable(instance_id)
    log(f"PG {version} log okunamıyor", {"tur": totals, "tür": kind, "gerekçe": reason})
    assert kind == "not_measured" and "log'u okunamıyor" in reason

    server_log = await admin.fetchval("SELECT pg_read_file($1, $2, (pg_stat_file($1)).size - $2)", logfile, offset)

    async def agent_returns_server_log(options, service, lines=100, timeout=5.0):
        return {"lines": server_log.splitlines()[-lines:]}

    monkeypatch.setattr(plan_capture, "fetch_agent_logs", agent_returns_server_log)
    await plan_capture.capture_plans_tick()
    kind, reason, _ = await _unavailable(instance_id)
    log(f"PG {version} log okunuyor", (kind, reason))
    assert kind == "no_plans_yet" and "Log okunuyor" in reason and "okunamadı" in reason
