"""Bloklama geçmişi ve deadlock tespiti (Faz 26 İŞ 3b).

Canlı ağaç "şu anda kim kimi blokluyor" sorusunu cevaplıyor. Bu dosya ikinci soruyu koruyor:
**"dün gece 03:14'te ne oldu."** En kötü olaylar kimsenin ekrana bakmadığı saatlerde yaşanır
ve sabah geriye kalan tek şey "gece sistem yavaştı" cümlesidir.

Deadlock ayrı bir dert: veritabanı döngüyü kendisi kırar ve olay ANLIKTIR — canlı ekranda
hiçbir izi kalmaz. Bir dakika sonra bakan biri hiçbir şey göremez. Yalnızca log'dan
görülebilir.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import BlockingEpisode, DeadlockEvent, Instance
from app.services import blocking_history
from app.services.blocking import build_blocking_tree
from app.services.blocking_history import (
    MIN_EPISODE_SECONDS,
    MISSING_ROUNDS_BEFORE_CLOSE,
    close_all,
    record_tree,
)
from app.services.credentials import encrypt_secret
from app.services.deadlocks import parse_postgres_deadlocks, parse_sqlserver_deadlock_xml
from app.services.plan_capture import store_deadlocks

BASE = datetime(2026, 9, 12, 3, 14, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    blocking_history.reset_state()
    yield
    blocking_history.reset_state()


async def _instance() -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"blk-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


def _rows(root_pid: int, blocked: int, *, idle: bool = False, transaction_seconds: float = 120.0):
    rows = [
        {
            "pid": root_pid,
            "username": "app",
            "application": "api",
            "state": "idle in transaction" if idle else "active",
            "query": "BEGIN" if idle else "UPDATE orders SET x = 1",
            "query_seconds": None if idle else 100.0,
            "transaction_seconds": transaction_seconds,
            "wait_seconds": None,
            "blocking_pids": [],
            "lock_type": "transactionid",
            "lock_mode": "ExclusiveLock",
            "lock_object": "orders",
            "held_locks": 12,
        }
    ]
    for i in range(blocked):
        rows.append(
            {
                "pid": root_pid + 1 + i,
                "username": "app",
                "application": "api",
                "state": "active",
                "query": "SELECT * FROM orders",
                "query_seconds": 10.0,
                "transaction_seconds": 10.0,
                "wait_seconds": 10.0,
                "blocking_pids": [root_pid],
                "lock_type": "transactionid",
                "lock_mode": "ShareLock",
                "lock_object": "orders",
                "held_locks": 0,
            }
        )
    return rows


# --- Olay yaşam döngüsü -------------------------------------------------------------------


async def test_an_episode_is_written_once_it_passes_the_minimum_duration():
    """Kilit beklemesi normal bir olaydır. Her milisaniyelik çakışmayı kaydetmek, tabloyu
    gürültüyle doldurup gerçek olayları görünmez yapardı."""
    instance = await _instance()
    tree = build_blocking_tree(_rows(100, 2, transaction_seconds=MIN_EPISODE_SECONDS + 10))
    async with SessionLocal() as session:
        counts = await record_tree(session, instance.id, tree, now=BASE)
        await session.commit()
        rows = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalars().all()
    assert counts["opened"] == 1
    assert len(rows) == 1
    assert rows[0].root_pid == 100
    assert rows[0].max_blocked_sessions == 2
    assert rows[0].ended_at is None, "olay sürerken bitmiş görünmemeli"


async def test_a_very_short_block_is_not_recorded():
    instance = await _instance()
    tree = build_blocking_tree(_rows(100, 1, transaction_seconds=1.0))
    async with SessionLocal() as session:
        await record_tree(session, instance.id, tree, now=BASE)
        await session.commit()
        rows = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalars().all()
    assert rows == []


async def test_peak_values_are_kept_not_the_last_reading():
    """Olayın etkisini "o an kaç oturum bekliyordu" değil "EN FAZLA kaç oturum bekledi"
    anlatır. Son okumayı saklamak, zirvesi geçmiş bir olayı zararsız gösterirdi."""
    instance = await _instance()
    async with SessionLocal() as session:
        await record_tree(session, instance.id, build_blocking_tree(_rows(100, 2)), now=BASE)
        await record_tree(
            session, instance.id, build_blocking_tree(_rows(100, 9)),
            now=BASE + timedelta(seconds=10),
        )
        await record_tree(
            session, instance.id, build_blocking_tree(_rows(100, 1)),
            now=BASE + timedelta(seconds=20),
        )
        await session.commit()
        row = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalar_one()
    assert row.max_blocked_sessions == 9


async def test_an_episode_is_closed_after_the_blocker_disappears():
    instance = await _instance()
    async with SessionLocal() as session:
        await record_tree(session, instance.id, build_blocking_tree(_rows(100, 3)), now=BASE)
        # Blokçu kayboldu — ama tek tur yetmiyor.
        for i in range(MISSING_ROUNDS_BEFORE_CLOSE):
            counts = await record_tree(
                session, instance.id, build_blocking_tree([]), now=BASE + timedelta(seconds=10 * (i + 1))
            )
        await session.commit()
        row = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalar_one()
    assert counts["closed"] == 1
    assert row.ended_at is not None
    assert row.duration_seconds > 0


async def test_a_single_missed_round_does_not_split_one_episode_into_two():
    """Tek turluk bir kayboluş ölçüm penceresine denk gelmemiş olabilir. Hemen kapatmak,
    tek bir olayı onlarca kısa parçaya bölerdi ve rapor okunamaz hâle gelirdi."""
    instance = await _instance()
    async with SessionLocal() as session:
        await record_tree(session, instance.id, build_blocking_tree(_rows(100, 3)), now=BASE)
        await record_tree(session, instance.id, build_blocking_tree([]), now=BASE + timedelta(seconds=10))
        await record_tree(
            session, instance.id, build_blocking_tree(_rows(100, 3)), now=BASE + timedelta(seconds=20)
        )
        await session.commit()
        rows = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].ended_at is None


async def test_the_idle_flag_survives_even_if_the_blocker_starts_running_later():
    """Kök engelleyici arada sorgu çalıştırmaya başlayabilir. "Sessizdi" bilgisi bir kez bile
    doğruysa korunuyor: teşhis açısından belirleyici olan odur (sorun uygulamada)."""
    instance = await _instance()
    async with SessionLocal() as session:
        await record_tree(
            session, instance.id, build_blocking_tree(_rows(100, 2, idle=True)), now=BASE
        )
        await record_tree(
            session, instance.id, build_blocking_tree(_rows(100, 2, idle=False)),
            now=BASE + timedelta(seconds=10),
        )
        for i in range(MISSING_ROUNDS_BEFORE_CLOSE):
            await record_tree(
                session, instance.id, build_blocking_tree([]),
                now=BASE + timedelta(seconds=20 + 10 * i),
            )
        await session.commit()
        row = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalar_one()
    assert row.root_was_idle is True


async def test_episode_start_is_estimated_from_transaction_age_not_first_sight():
    """Örnekleme aralığı 10 saniye. "İlk gördüğümüz an" demek, olayları sistematik olarak
    kısa gösterirdi; bekletme kilidin alındığı anda başlar."""
    instance = await _instance()
    tree = build_blocking_tree(_rows(100, 2, transaction_seconds=300.0))
    async with SessionLocal() as session:
        await record_tree(session, instance.id, tree, now=BASE)
        await session.commit()
        row = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalar_one()
    started = row.started_at.replace(tzinfo=UTC) if row.started_at.tzinfo is None else row.started_at
    assert (BASE - started).total_seconds() == pytest.approx(300.0, abs=1.0)


async def test_shutdown_closes_open_episodes():
    """Kapanışta kapatılmazsa `ended_at` sonsuza kadar NULL kalır ve olay raporlarda "hâlâ
    sürüyor" gibi görünür."""
    instance = await _instance()
    async with SessionLocal() as session:
        await record_tree(session, instance.id, build_blocking_tree(_rows(100, 2)), now=BASE)
        closed = await close_all(session)
        await session.commit()
        row = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalar_one()
    assert closed == 1
    assert row.ended_at is not None


async def test_two_independent_blockers_produce_two_episodes():
    instance = await _instance()
    rows = _rows(100, 2) + _rows(200, 3)
    async with SessionLocal() as session:
        counts = await record_tree(session, instance.id, build_blocking_tree(rows), now=BASE)
        await session.commit()
        episodes = (await session.execute(select(BlockingEpisode).where(BlockingEpisode.instance_id == instance.id))).scalars().all()
    assert counts["opened"] == 2
    assert {e.root_pid for e in episodes} == {100, 200}


# --- PostgreSQL deadlock ayrıştırma -------------------------------------------------------


PG_DEADLOCK_LOG = [
    "2026-09-12 03:14:07.123 UTC [4211] ERROR:  deadlock detected",
    "2026-09-12 03:14:07.123 UTC [4211] DETAIL:  Process 4211 waits for ShareLock on transaction 991; blocked by process 4299.",
    "\tProcess 4299 waits for ShareLock on transaction 990; blocked by process 4211.",
    "\tProcess 4211: UPDATE accounts SET balance = balance - 10 WHERE id = 1",
    "\tProcess 4299: UPDATE accounts SET balance = balance + 10 WHERE id = 2",
    "2026-09-12 03:14:07.123 UTC [4211] HINT:  See server log for query details.",
    "2026-09-12 03:14:07.123 UTC [4211] STATEMENT:  UPDATE accounts SET balance = balance - 10 WHERE id = 1",
]


def test_a_postgres_deadlock_is_parsed_with_victim_and_winner():
    """Yalnızca kurbanı göstermek YARIM teşhistir: kurbanın suçu genelde yoktur, döngüyü
    oluşturan kilit sırası kazananındır ve düzeltme orada yapılır."""
    records = parse_postgres_deadlocks(PG_DEADLOCK_LOG)
    assert len(records) == 1
    record = records[0]
    assert record.victim_pid == 4211
    assert "balance - 10" in record.victim_query
    assert record.winner_pid == 4299
    assert "balance + 10" in record.winner_query
    assert record.participants == 2


def test_deadlock_timestamp_comes_from_the_log_line():
    record = parse_postgres_deadlocks(PG_DEADLOCK_LOG)[0]
    assert record.detected_at.hour == 3 and record.detected_at.minute == 14


def test_the_lock_cycle_edges_are_captured():
    record = parse_postgres_deadlocks(PG_DEADLOCK_LOG)[0]
    assert (4211, "ShareLock", "transaction 991", 4299) in record.edges
    assert len(record.edges) == 2


def test_two_deadlocks_in_one_window_are_both_found():
    records = parse_postgres_deadlocks(PG_DEADLOCK_LOG + PG_DEADLOCK_LOG)
    assert len(records) == 2


def test_logs_without_deadlocks_produce_nothing():
    assert parse_postgres_deadlocks(["2026-09-12 03:00:00 UTC [1] LOG:  checkpoint starting"]) == []


def test_the_fingerprint_is_built_from_queries_not_pids():
    """pid'ler her deadlock'ta farklıdır ama aynı deadlock TEKRAR ETTİĞİNDE sorgular aynıdır.
    Anahtarı pid'den türetmek hem tekrar tespitini bozar hem de "aynı deadlock 40 kez oldu"
    sorusunu cevaplanamaz kılar."""
    first = parse_postgres_deadlocks(PG_DEADLOCK_LOG)[0]
    other_pids = [line.replace("4211", "7777").replace("4299", "8888") for line in PG_DEADLOCK_LOG]
    second = parse_postgres_deadlocks(other_pids)[0]
    assert first.fingerprint == second.fingerprint


async def test_the_same_deadlock_is_stored_once():
    """Log penceresi her çekimde örtüşüyor; aynı olay birden çok kez görülüyor."""
    instance = await _instance()
    records = parse_postgres_deadlocks(PG_DEADLOCK_LOG)
    async with SessionLocal() as session:
        first = await store_deadlocks(session, instance.id, records)
        await session.commit()
    async with SessionLocal() as session:
        second = await store_deadlocks(session, instance.id, records)
        await session.commit()
        rows = (await session.execute(select(DeadlockEvent).where(DeadlockEvent.instance_id == instance.id))).scalars().all()
    assert first == 1 and second == 0
    assert len(rows) == 1


# --- SQL Server deadlock ------------------------------------------------------------------


SQLSERVER_XML = """
<deadlock>
  <victim-list><victimProcess id="process1" /></victim-list>
  <process-list>
    <process id="process1" spid="61"><inputbuf>UPDATE accounts SET balance = 1</inputbuf></process>
    <process id="process2" spid="62"><inputbuf>UPDATE accounts SET balance = 2</inputbuf></process>
  </process-list>
</deadlock>
"""


def test_sqlserver_victim_is_read_from_the_victim_list_not_guessed():
    """PostgreSQL'in aksine SQL Server kurbanı XML'de AÇIKÇA işaretler; tahmin etmek
    gereksiz bir hata kaynağı olurdu."""
    record = parse_sqlserver_deadlock_xml(SQLSERVER_XML, BASE)
    assert record is not None
    assert record.victim_pid == 61
    assert "balance = 1" in record.victim_query
    assert record.winner_pid == 62
    assert record.source == "sqlserver_system_health"


def test_malformed_deadlock_xml_returns_none_instead_of_raising():
    """Bozuk bir XML tüm toplama turunu düşürmemeli."""
    assert parse_sqlserver_deadlock_xml("<deadlock><unclosed>", BASE) is None
