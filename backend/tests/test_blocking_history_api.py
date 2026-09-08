"""Bloklama geçmişi ucu ve SQL Server deadlock toplama (Faz 26 İŞ 3c).

BU DOSYA ÜÇ BOŞLUĞU KAPATIYOR — üçü de "yazıldı ama bağlanmadı" cinsindendi:

1. **SQL Server deadlock toplama ölü koddu.** `SQLSERVER_DEADLOCK_SQL` ve
   `parse_sqlserver_deadlock_xml` yazılmış ve test edilmişti ama üretim kodunda hiçbir yerden
   çağrılmıyordu. PostgreSQL deadlock'ları log çekiminden geliyordu; SQL Server tarafı
   sessizce hiç çalışmıyordu.
2. **Kurban ve kazanan SORGULARI hiçbir yerde görünmüyordu.** Rapor yalnızca pid taşıyordu ve
   pid olaydan sonra hiçbir şey ifade etmiyor — süreç çoktan kapanmış oluyor.
3. **Toplanan geçmiş yalnızca günlük raporda görünüyordu.** Veri birikiyordu ama kullanıcı
   ekrandan ulaşamıyordu.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import BlockingEpisode, DeadlockEvent, Instance
from app.services.credentials import encrypt_secret
from app.services.deadlocks import parse_sqlserver_deadlock_rows
from tests.auth_helper import authed_client

NOW = datetime.now(UTC)

SQLSERVER_XML = """
<deadlock>
  <victim-list><victimProcess id="process1" /></victim-list>
  <process-list>
    <process id="process1" spid="61"><inputbuf>UPDATE accounts SET balance = 1 WHERE id = 7</inputbuf></process>
    <process id="process2" spid="62"><inputbuf>UPDATE accounts SET balance = 2 WHERE id = 8</inputbuf></process>
  </process-list>
</deadlock>
"""


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(engine: str = "postgresql") -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"hist-{uuid.uuid4().hex[:8]}", engine=engine, host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _seed_episode(instance: Instance, **over) -> BlockingEpisode:
    async with SessionLocal() as session:
        row = BlockingEpisode(
            instance_id=instance.id,
            started_at=over.pop("started_at", NOW - timedelta(hours=2)),
            ended_at=over.pop("ended_at", NOW - timedelta(hours=1, minutes=55)),
            duration_seconds=over.pop("duration_seconds", 300.0),
            root_pid=over.pop("root_pid", 4242),
            root_query=over.pop("root_query", "UPDATE orders SET status = 'x'"),
            root_was_idle=over.pop("root_was_idle", False),
            max_blocked_sessions=over.pop("max_blocked_sessions", 7),
            max_chain_depth=over.pop("max_chain_depth", 2),
            lock_object=over.pop("lock_object", "orders"),
            **over,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _seed_deadlock(instance: Instance, **over) -> DeadlockEvent:
    async with SessionLocal() as session:
        row = DeadlockEvent(
            instance_id=instance.id,
            detected_at=over.pop("detected_at", NOW - timedelta(hours=3)),
            source=over.pop("source", "postgresql_log"),
            fingerprint=over.pop("fingerprint", uuid.uuid4().hex[:16]),
            victim_pid=over.pop("victim_pid", 111),
            victim_query=over.pop("victim_query", "UPDATE accounts SET balance = balance - 10"),
            winner_pid=over.pop("winner_pid", 222),
            winner_query=over.pop("winner_query", "UPDATE accounts SET balance = balance + 10"),
            participants=over.pop("participants", 2),
            **over,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


# --- Geçmiş ucu ---------------------------------------------------------------------------


async def test_the_history_endpoint_returns_episodes_and_deadlocks():
    """Toplanan geçmiş yalnızca günlük raporda görünüyordu; kullanıcı ekrandan
    ulaşamıyordu."""
    instance = await _instance()
    await _seed_episode(instance)
    await _seed_deadlock(instance)

    async with await authed_client() as client:
        response = await client.get(f"/api/instances/{instance.id}/blocking-history")

    assert response.status_code == 200
    body = response.json()
    assert len(body["episodes"]) == 1
    assert len(body["deadlocks"]) == 1
    assert body["unavailable_reason"] is None


async def test_deadlocks_carry_both_victim_and_winner_queries():
    """YALNIZCA KURBANI GÖSTERMEK YARIM TEŞHİSTİR. Kurbanın "suçu" genelde yoktur; döngüyü
    oluşturan kilit sırası kazananındır ve düzeltme orada yapılır. Ayrıca pid olaydan sonra
    hiçbir şey ifade etmiyor — süreç çoktan kapanmış oluyor."""
    instance = await _instance()
    await _seed_deadlock(instance)

    async with await authed_client() as client:
        body = (
            await client.get(f"/api/instances/{instance.id}/blocking-history")
        ).json()

    event = body["deadlocks"][0]
    assert "balance - 10" in event["victim_query"]
    assert "balance + 10" in event["winner_query"]
    assert event["victim_pid"] == 111
    assert event["winner_pid"] == 222


async def test_the_idle_root_blocker_flag_survives_to_the_api():
    """Raporun en değerli ayrımı: kök engelleyici sorgu çalıştırmıyorduysa sorun
    veritabanında değil uygulamadadır."""
    instance = await _instance()
    await _seed_episode(instance, root_was_idle=True, root_query="BEGIN")

    async with await authed_client() as client:
        body = (await client.get(f"/api/instances/{instance.id}/blocking-history")).json()
    assert body["episodes"][0]["root_was_idle"] is True


async def test_an_ongoing_episode_has_no_end_time():
    instance = await _instance()
    await _seed_episode(instance, ended_at=None)
    async with await authed_client() as client:
        body = (await client.get(f"/api/instances/{instance.id}/blocking-history")).json()
    assert body["episodes"][0]["ended_at"] is None


async def test_the_time_window_is_respected():
    instance = await _instance()
    await _seed_episode(instance, started_at=NOW - timedelta(days=20))
    async with await authed_client() as client:
        body = (
            await client.get(f"/api/instances/{instance.id}/blocking-history?hours=24")
        ).json()
    assert body["episodes"] == []


async def test_no_events_says_why_instead_of_returning_a_bare_empty_list():
    """"Bloklama olmadı" ile "bloklama ölçülmedi" farklı şeyler; ikincisini sessizce boş
    liste göstermek kullanıcıya yanlış bir güvence verirdi."""
    instance = await _instance()
    async with await authed_client() as client:
        body = (await client.get(f"/api/instances/{instance.id}/blocking-history")).json()
    assert body["episodes"] == [] and body["deadlocks"] == []
    assert body["unavailable_reason"]
    # Kısa beklemelerin bilerek kaydedilmediği de söyleniyor — kullanıcı "hiç mi olmadı"
    # diye şüphelenmesin.
    assert "5 saniye" in body["unavailable_reason"]


async def test_mongodb_says_the_feature_does_not_apply():
    instance = await _instance(engine="mongodb")
    async with await authed_client() as client:
        body = (await client.get(f"/api/instances/{instance.id}/blocking-history")).json()
    assert "MongoDB" in body["unavailable_reason"]


async def test_a_deleted_instance_returns_404_not_an_empty_history():
    async with await authed_client() as client:
        response = await client.get("/api/instances/999999/blocking-history")
    assert response.status_code == 404


async def test_history_is_scoped_to_the_requested_instance():
    """Başka bir instance'ın olaylarını göstermek, kullanıcıyı yanlış sunucuda sorun
    aramaya yollardı."""
    first = await _instance()
    second = await _instance()
    await _seed_episode(first)
    await _seed_deadlock(second)

    async with await authed_client() as client:
        body = (await client.get(f"/api/instances/{first.id}/blocking-history")).json()
    assert len(body["episodes"]) == 1
    assert body["deadlocks"] == []


# --- SQL Server deadlock toplama artık bağlı ------------------------------------------------


def test_sqlserver_rows_are_parsed_into_records():
    rows = [{"detected_at": "2026-09-14T03:14:07.123", "deadlock_xml": SQLSERVER_XML}]
    records = parse_sqlserver_deadlock_rows(rows)
    assert len(records) == 1
    record = records[0]
    assert record.victim_pid == 61
    assert record.winner_pid == 62
    assert record.source == "sqlserver_system_health"


def test_the_xe_timestamp_is_read_as_utc_not_shifted():
    """XE zaman damgası ZATEN UTC. İlk yazımda sorgu `DATEADD` ile yerel saate çeviriyordu
    ve sonuç UTC gibi saklanacaktı — saat farkı kadar kaymış deadlock kayıtları demek."""
    rows = [{"detected_at": "2026-09-14T03:14:07.123", "deadlock_xml": SQLSERVER_XML}]
    record = parse_sqlserver_deadlock_rows(rows)[0]
    assert record.detected_at.tzinfo is UTC
    assert (record.detected_at.hour, record.detected_at.minute) == (3, 14)


def test_the_capture_sql_does_not_shift_the_timestamp():
    from app.services.deadlocks import SQLSERVER_DEADLOCK_SQL

    assert "DATEADD" not in SQLSERVER_DEADLOCK_SQL
    assert "system_health" in SQLSERVER_DEADLOCK_SQL


def test_one_broken_xml_does_not_drop_the_whole_batch():
    """system_health halka tamponu döngüsel; orada her zaman yarım kalmış olaylar
    bulunabiliyor. Biri yüzünden turun tamamını kaybetmek kabul edilemez."""
    rows = [
        {"detected_at": "2026-09-14T03:00:00", "deadlock_xml": "<deadlock><unclosed>"},
        {"detected_at": "2026-09-14T03:14:07", "deadlock_xml": SQLSERVER_XML},
    ]
    records = parse_sqlserver_deadlock_rows(rows)
    assert len(records) == 1
    assert records[0].victim_pid == 61


def test_rows_without_xml_are_skipped():
    assert parse_sqlserver_deadlock_rows([{"detected_at": "2026-09-14T03:00:00"}]) == []


async def test_sqlserver_deadlock_capture_is_actually_wired(monkeypatch):
    """BU TESTİN VARLIK SEBEBİ: toplama kodu yazılmış ve test edilmişti ama üretim kodunda
    HİÇBİR YERDEN çağrılmıyordu — ölü koddu. Test artık zincirin bağlı olduğunu doğruluyor."""
    import app.services.plan_capture as plan_capture

    instance = await _instance(engine="sqlserver")

    class FakeCollector:
        async def collect_deadlocks(self, limit: int = 20):
            return [{"detected_at": "2026-09-14T03:14:07.123", "deadlock_xml": SQLSERVER_XML}]

    monkeypatch.setattr(plan_capture, "get_collector", lambda engine, target: FakeCollector())

    async with SessionLocal() as session:
        outcome = await plan_capture.capture_sqlserver_deadlocks(session, instance)
        await session.commit()
        stored = (
            await session.execute(
                select(DeadlockEvent).where(DeadlockEvent.instance_id == instance.id)
            )
        ).scalars().all()

    assert outcome["error"] is None
    assert outcome["found"] == 1 and outcome["written"] == 1
    assert stored[0].victim_pid == 61
    assert stored[0].source == "sqlserver_system_health"


async def test_a_failing_xe_session_does_not_break_the_tick(monkeypatch):
    """XE oturumu kapalı olabilir ya da yetki yetmeyebilir; deadlock geçmişi bir EK yetenek
    ve toplamanın geri kalanını düşürmemeli."""
    import app.services.plan_capture as plan_capture

    instance = await _instance(engine="sqlserver")

    class FailingCollector:
        async def collect_deadlocks(self, limit: int = 20):
            raise RuntimeError("VIEW SERVER STATE yetkisi yok")

    monkeypatch.setattr(plan_capture, "get_collector", lambda engine, target: FailingCollector())

    async with SessionLocal() as session:
        outcome = await plan_capture.capture_sqlserver_deadlocks(session, instance)

    assert outcome["error"] and "yetkisi yok" in outcome["error"]
    assert outcome["written"] == 0


async def test_postgres_instances_are_refused_by_the_sqlserver_path():
    instance = await _instance(engine="postgresql")
    import app.services.plan_capture as plan_capture

    async with SessionLocal() as session:
        outcome = await plan_capture.capture_sqlserver_deadlocks(session, instance)
    assert "SQL Server" in outcome["error"]
