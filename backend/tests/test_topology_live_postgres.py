"""Tek sunucuda cluster alarmı YOK, replika koparılınca VAR, yetki eksikse "ölçülemedi" — gerçek sunucu
(Faz 31 Commit 6).

Kurulum (scripts/live_pg.py): her sürümde birincil + streaming replika. Replika SQL'le koparılıyor
(`ALTER SYSTEM SET primary_conninfo = ''`) ve her testten sonra geri bağlanıyor — docker CLI gerekmiyor.

Yol: veritabanı sihirbazın API'siyle (standalone grup — hatanın üretildiği yol) ya da düğüm/rol
ayrımı gereken yerde doğrudan ekleniyor → GERÇEK toplama turu (`collection.collect_instance`) →
GET /api/instances/{id}/cluster-health `topology` → alarm olayları dbace veritabanından.

"Tek sunucu" testi bu makinede kurulu GERÇEK host-agent'ı (agents/host-agent, alt süreç) bağlıyor:
hatanın tetikleyicisi agent'ın sunucuda olmayan patroni/etcd birimleri için döndürdüğü durumdu.

Bütün topoloji okumaları KISITLI rollerle: pg_monitor'lu `dbace_it_monitor`, hiçbir izleme yetkisi
olmayan `dbace_it_app`.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import AlertEvent, AlertRule, Instance
from app.services import collection as collection_module
from app.services import server_topology as server_topology_module
from app.services.credentials import encrypt_secret
from app.services.server_topology import DEGRADED_METRIC
from tests.live_pg import LIVE_DSNS, ROLE_PASSWORD, SKIP_REASON, dsn_id, prepare_live_database, replica_for, target_for

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

AGENT_DIR = Path(__file__).resolve().parents[2] / "agents" / "host-agent"
AGENT_TOKEN = "topology-it"
#: Faz 32 Commit 12c: setup'ta akışa geçemeyen replikayı SIFIRDAN yeniden kurar (bkz. `replica` fixture).
LIVE_PG_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_pg.py"


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


#: Teardown'ın "geri bağlan ve akışa dön" beklemesi (Faz 31 Commit 10f). 30 sn PG18'de CI'da yetersiz kaldı.
_REPLICA_RECONNECT_TIMEOUT = 180.0

#: Setup'ın "hâlâ akışta mı" denetimi (Faz 31 Commit 10g). `pg_stat_wal_receiver` HER sürümde, GERÇEKTEN
#: ve TEKRARLANABİLİR biçimde geçici olarak BOŞ dönüyor — ağır bir yazma yükü (`test_migration_scale_
#: live_postgres.py`'nin 420 bin satırlık backfill'i + CONCURRENTLY index kurulumu, dosya adı sırasıyla
#: bu dosyadan HEMEN ÖNCE koşuyor) sürerken birincildeki checkpoint/fsync baskısı replikanın wal
#: receiver'ını kısa süreliğine KOPARIP YENİDEN BAĞLIYOR — PostgreSQL'in normal davranışı. Yerelde
#: doğrudan ölçüldü: 1-4 sn'lik birkaç ölçüm BOŞ döndü, sonra kendiliğinden 'streaming'e döndü.
#:
#: Faz 32 Commit 12c: BU patience bütçesi KENDİSİ yetmeyen, GERÇEK bir CI çöküşüyle (PG17 kolu) ortaya
#: çıktı — replika 20 sn'nin TAMAMI boyunca hiç akışa geçmedi. Kök neden GERÇEKTEN kanıtlandı (yerelde
#: aynı çöküş bire bir üretildi, replikanın log'unda `FATAL: could not receive data from WAL stream:
#: ERROR: requested WAL segment ... has already been removed` görüldü): replika slot'suzdu, birincil
#: kopuk replikanın HENÜZ okumadığı WAL'ı bir checkpoint'te SİLDİ — bu noktadan sonra replika KENDİ
#: KENDİNE ASLA katılamaz (pg_basebackup'la sıfırdan kurulması gerekir), ne kadar beklenirse beklensin.
#: İki savunma eklendi: (1) `scripts/live_pg.py` artık replikayı FİZİKSEL REPLİKASYON SLOTUYLA kuruyor
#: — birincil artık replika okuyana kadar WAL'ı TUTAR, bu sınıftaki çöküşü KÖKTEN önler. (2) slot
#: kurulduktan SONRA da (başka bir nedenle) bozulursa, aşağıdaki `replica` fixture'ı KENDİ KENDİNİ
#: onarır: `scripts/live_pg.py rebuild` ile replikayı sıfırdan yeniden kurar — "elle onarın" demeyi
#: beklemez. Bu sabit süre artık "genuine arıza"yı hızlı yakalamak değil, SADECE geçici dalgalanma için
#: sabırlı olmak — genuine arıza artık rebuild ile OTOMATİK çözülüyor.
_REPLICA_SETUP_TIMEOUT = 20.0


@pytest.fixture
async def replica(dsn):
    """Replikanın süper kullanıcı bağlantısı — yalnızca TEST ALTYAPISI koparıp bağlamak için; dbace
    bu bağlantıyı kullanmıyor. Test sonunda bağlantı her durumda geri yazılıyor."""
    conn = await asyncpg.connect(replica_for(dsn), statement_cache_size=0)
    original = await conn.fetchval("SHOW primary_conninfo")
    assert original, "replika primary_conninfo boş — `python scripts/live_pg.py up` geri bağlar"
    # Yalnızca conninfo'nun dolu olması yetmez: ÖNCEKİ bir testin teardown'u geri yazıp akışa dönmeyi
    # BEKLEYEMEDEN (bkz. _REPLICA_RECONNECT_TIMEOUT) pes etmiş olabilir — bu durumda conninfo doğru değeri
    # taşır ama wal receiver henüz akışta değildir. Bunu burada, KURULUMDA, açık bir gerekçeyle yakala; aksi
    # hâlde bir sonraki test kendi gövdesinde ilgisiz bir asserte çarpıp neden başarısız olduğunu gizler.
    # Sabırlı (bkz. _REPLICA_SETUP_TIMEOUT) — tek ölçümlü assert, ağır yük altındaki GEÇİCİ boş pencereyi
    # (ölçüldü, yukarıdaki not) kalıcı arızayla karıştırırdı.
    try:
        await _wait(lambda: conn.fetchval("SELECT status FROM pg_stat_wal_receiver"), "streaming",
                   timeout=_REPLICA_SETUP_TIMEOUT)
    except AssertionError as exc:
        # Faz 32 Commit 12c: "elle onarın" demek yerine KENDİ KENDİNİ onar — slotla bile kurtarılamamış
        # (ör. WAL segmenti slot kurulmadan ÖNCEki bir koşuda zaten silinmiş, ya da slot başka bir
        # nedenle bozulmuş) bir replikayı sıfırdan yeniden kurar.
        print(f"\n  [replica setup] akışta değil ({exc}) — sıfırdan yeniden kuruluyor (rebuild)")
        await conn.close()
        port = urlparse(replica_for(dsn)).port
        rebuild = subprocess.run(
            [sys.executable, str(LIVE_PG_SCRIPT), "rebuild", "--port", str(port)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        print(f"  [rebuild çıktısı] {rebuild.stdout}\n{rebuild.stderr}")
        if rebuild.returncode != 0:
            raise AssertionError(
                f"replika rebuild BAŞARISIZ (çıkış {rebuild.returncode}) — `scripts/live_pg.py` dışında "
                "bir sorun olabilir (docker ağı/disk); yukarıdaki çıktıyı elle inceleyin"
            ) from None
        conn = await asyncpg.connect(replica_for(dsn), statement_cache_size=0)
        # `rebuild` zaten KENDİ İÇİNDE akışa geçene kadar bekleyip döndü (scripts/live_pg.py::up_replica) —
        # bu yalnızca YENİ bağlantının aynı görüşü paylaştığını doğruluyor, kısa bir bütçe yeter.
        await _wait(lambda: conn.fetchval("SELECT status FROM pg_stat_wal_receiver"), "streaming", timeout=10.0)
        original = await conn.fetchval("SHOW primary_conninfo")
    try:
        yield conn
    finally:
        await conn.execute(await conn.fetchval("SELECT format('ALTER SYSTEM SET primary_conninfo = %L', $1::text)", original))
        await conn.execute("SELECT pg_reload_conf()")
        await _wait(lambda: conn.fetchval("SELECT status FROM pg_stat_wal_receiver"), "streaming",
                   timeout=_REPLICA_RECONNECT_TIMEOUT)
        await conn.close()


@pytest.fixture(scope="module")
def host_agent():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=AGENT_DIR, env={**os.environ, "AGENT_TOKEN": AGENT_TOKEN},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/v1/services", headers={"X-Agent-Token": AGENT_TOKEN}, timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        services = httpx.get(f"{url}/v1/services", headers={"X-Agent-Token": AGENT_TOKEN}, timeout=10).json()["services"]
        assert all(v["active"] != "active" for v in services.values()), services
        yield {"agent_url": url, "agent_token": AGENT_TOKEN, "services": services}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def _wait(fetch, expected, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = await fetch()
        if value == expected:
            return value
        await asyncio.sleep(0.5)
    raise AssertionError(f"{timeout} sn içinde beklenen {expected!r} olmadı (son: {value!r})")


async def _break(replica_conn, primary_admin):
    await replica_conn.execute("ALTER SYSTEM SET primary_conninfo = ''")
    await replica_conn.execute("SELECT pg_reload_conf()")
    await _wait(lambda: primary_admin.fetchval("SELECT count(*) FROM pg_stat_replication"), 0)


def _direct_instance(dsn: str, role: str, **extra) -> Instance:
    t = target_for(dsn, role)
    return Instance(name=f"topo-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
                    database=t.database, username=t.username, password=encrypt_secret(ROLE_PASSWORD), enabled=True, **extra)


async def _add(instance: Instance) -> int:
    await init_db()
    async with SessionLocal() as session:
        session.add(instance)
        await session.commit()
        return instance.id


async def _wizard_standalone(dsn: str, role: str, agent: dict) -> int:
    from tests.auth_helper import authed_client

    t = target_for(dsn, role)
    await init_db()
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": f"topo-{uuid.uuid4().hex[:8]}"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        body = {
            "application_id": application["id"], "group_name": f"tek-{uuid.uuid4().hex[:6]}", "engine": "postgresql",
            "topology": "standalone",
            "nodes": [{"server_name": f"srv-{uuid.uuid4().hex[:6]}", "host": t.host, "port": t.port, "database": t.database,
                       "db_username": t.username, "db_password": ROLE_PASSWORD, "agent_url": agent["agent_url"],
                       "agent_token": agent["agent_token"]}],
        }
        response = await client.post("/api/wizard/database-groups", json=body)
        assert response.status_code == 201, response.text
        nodes = (await client.get(f"/api/groups/{response.json()['id']}/nodes")).json()
    instance_id = nodes[0]["instance_id"]
    async with SessionLocal() as session:
        instance = await session.get(Instance, instance_id)
        # Plan yakalama için tek sunucuya kurulan agent (hatanın tetikleyicisi) — instance düzeyinde.
        instance.options = {**(instance.options or {}), "agent_url": agent["agent_url"], "agent_token": agent["agent_token"]}
        await session.commit()
    return instance_id


async def _collect(instance_id: int) -> None:
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()


async def _state(instance_id: int) -> dict:
    from tests.auth_helper import authed_client

    async with await authed_client() as client:
        response = await client.get(f"/api/instances/{instance_id}/cluster-health")
    assert response.status_code == 200, response.text
    body = response.json()
    async with SessionLocal() as session:
        events = (await session.execute(
            select(AlertRule.metric).join(AlertEvent, AlertEvent.rule_id == AlertRule.id)
            .where(AlertEvent.instance_id == instance_id, AlertEvent.resolved_at.is_(None))
        )).scalars().all()
        rules = (await session.execute(select(AlertRule.metric).where(AlertRule.instance_id == instance_id))).scalars().all()
    return {"topology": body["topology"], "overall": body["overall"], "totals": body["totals"],
            "services": [(s["service"], s["status"]) for s in body["services"]],
            "events": sorted(events), "rules": sorted(rules)}


async def _version(admin) -> str:
    return (await admin.fetchval("SHOW server_version")).split(" ")[0]


async def test_standalone_with_real_host_agent_has_no_cluster_alarm(admin, replica, dsn, host_agent):
    await _break(replica, admin)  # birincil artık replikasız: tek sunucu
    instance_id = await _wizard_standalone(dsn, "monitor", host_agent)
    await _collect(instance_id)
    await _collect(instance_id)
    state = await _state(instance_id)
    log(f"PG {await _version(admin)} tek sunucu + gerçek agent", state | {"agent": {k: v["active"] for k, v in host_agent["services"].items()}})
    assert state["topology"]["kind"] == "standalone", state
    assert state["events"] == [] and state["rules"] == []
    assert state["totals"]["down"] == 0 and state["overall"] == "healthy"
    assert [name for name, _ in state["services"]] == ["postgresql"]


async def test_breaking_the_real_replica_raises_the_alarm_on_primary_and_replica(admin, replica, dsn, monkeypatch):
    """Faz 32 Commit 12c: GERÇEK sunucuda doğrulanan görev 2a'nın sorusu — "geçici bir kopmada 'replika
    BAĞLI DEĞİL' alarmı veriyor mu?" YANITI: HAYIR, ilk ölçümde vermiyor (aşağıdaki `after_first`) — grace
    period (`DEGRADED_CONFIRM_AFTER`) kadar süregelen bozulmadan sonra veriyor (`after_confirmed`). Eşik
    testte hızlı olsun diye 2 sn'ye küçültüldü (üretimde 60 sn) — AYNI `record_topology()` kod yolu,
    yalnızca süre kısaltıldı (bkz. `test_statement_timeout_cancels_a_long_analyze`'deki aynı desen)."""
    monkeypatch.setattr(server_topology_module, "DEGRADED_CONFIRM_AFTER", timedelta(seconds=2))
    version = await _version(admin)
    primary_id = await _add(_direct_instance(dsn, "monitor"))
    replica_id = await _add(_direct_instance(replica_for(dsn), "monitor"))
    for iid in (primary_id, replica_id):
        await _collect(iid)
    before = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} bağlı", {k: (v["topology"]["kind"], v["topology"]["state"], v["topology"]["role"], v["events"]) for k, v in before.items()})
    for view in before.values():
        assert (view["topology"]["kind"], view["topology"]["state"]) == ("cluster", "healthy")
        assert view["events"] == [] and DEGRADED_METRIC in view["rules"]

    await _break(replica, admin)
    await _wait(lambda: replica.fetchval("SELECT count(*) FROM pg_stat_wal_receiver"), 0)

    # 1) İLK ölçüm, kopma HENÜZ grace period'un altında: alarm YOK, ama "geçici kopma olabilir" görünür.
    for iid in (primary_id, replica_id):
        await _collect(iid)
    after_first = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} koparıldı (ilk ölçüm, henüz doğrulanmadı)",
        {k: (v["topology"]["kind"], v["topology"]["state"], v["topology"]["reason"], v["events"]) for k, v in after_first.items()})
    for view in after_first.values():
        assert (view["topology"]["kind"], view["topology"]["state"]) == ("cluster", "degraded")
        assert view["events"] == [], "TEK ölçümle alarm üretilmemeliydi (CLAUDE.md: süre eşiği/mekanizma kuralı)"
        assert "Geçici" in view["topology"]["reason"]

    # 2) Grace period (2 sn) AŞILDI, kopma HÂLÂ sürüyor: şimdi gerçek alarm.
    await asyncio.sleep(2.2)
    for iid in (primary_id, replica_id):
        await _collect(iid)
    after_confirmed = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} koparıldı (doğrulandı)",
        {k: (v["topology"]["kind"], v["topology"]["state"], v["topology"]["reason"], v["events"]) for k, v in after_confirmed.items()})
    for view in after_confirmed.values():
        assert (view["topology"]["kind"], view["topology"]["state"]) == ("cluster", "degraded")
        assert view["events"] == [DEGRADED_METRIC]
        assert view["topology"]["transient_disconnect_count"] == 0, "bu kopma hiç iyileşmedi — geçici SAYILMAMALI"


async def test_negative_control_a_transient_disconnect_that_heals_before_confirmation_never_alarms(admin, replica, dsn, monkeypatch):
    """Faz 32 Commit 12c — görev 2d madde 2 (negatif kontrol, GERÇEK sunucuda): grace period İÇİNDE
    kendiliğinden iyileşen bir kopma HİÇ alarm üretmemeli, ama DBA'nın görmesi için SAYILMALI."""
    monkeypatch.setattr(server_topology_module, "DEGRADED_CONFIRM_AFTER", timedelta(seconds=5))
    version = await _version(admin)
    replica_id = await _add(_direct_instance(replica_for(dsn), "monitor"))
    await _collect(replica_id)
    original = await replica.fetchval("SHOW primary_conninfo")

    await _break(replica, admin)
    await _wait(lambda: replica.fetchval("SELECT count(*) FROM pg_stat_wal_receiver"), 0)
    await _collect(replica_id)
    mid = await _state(replica_id)
    assert (mid["topology"]["kind"], mid["topology"]["state"]) == ("cluster", "degraded")
    assert mid["events"] == [], "grace period içinde alarm üretilmemeliydi"

    # Eşik (5 sn) DOLMADAN geri bağla.
    await replica.execute(await replica.fetchval("SELECT format('ALTER SYSTEM SET primary_conninfo = %L', $1::text)", original))
    await replica.execute("SELECT pg_reload_conf()")
    await _wait(lambda: replica.fetchval("SELECT status FROM pg_stat_wal_receiver"), "streaming", timeout=10.0)
    await _collect(replica_id)
    healed = await _state(replica_id)
    log(f"PG {version} eşiğe ulaşmadan iyileşti",
        (healed["topology"]["kind"], healed["topology"]["state"], healed["topology"]["transient_disconnect_count"], healed["events"]))
    assert healed["events"] == [], "hiçbir zaman alarm üretilmemiş olmalı"
    assert healed["topology"]["transient_disconnect_count"] == 1, "eşiğe ulaşmadan iyileşen kopma SAYILMALI"


async def test_missing_privilege_is_unmeasured_with_the_grant_and_no_alarm(admin, replica, dsn):
    version = await _version(admin)
    primary_id = await _add(_direct_instance(dsn, "app"))
    replica_id = await _add(_direct_instance(replica_for(dsn), "app"))
    for iid in (primary_id, replica_id):
        await _collect(iid)
    views = {"birincil": await _state(primary_id), "replika": await _state(replica_id)}
    log(f"PG {version} pg_monitor'suz rol", {k: (v["topology"]["kind"], v["topology"]["reason"], v["topology"]["required_grant"], v["events"]) for k, v in views.items()})
    for view in views.values():
        assert view["topology"]["kind"] == "unmeasured"
        assert "pg_monitor" in view["topology"]["required_grant"]
        assert view["events"] == [] and view["rules"] == []
