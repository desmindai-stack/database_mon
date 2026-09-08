"""Aktif oturum örnekleyicisi (Faz 25 İŞ 1).

Testlerin koruduğu asıl şey, örnekleyicinin İKİ SINIRI:

1. **Ham örnek saklanmaz.** 1 saniyelik örnekleme, dakikada 60 tur demek; bu turların
   veritabanına 60 satır olarak düşmesi kabul edilemez. Testler 60 turun tek satıra
   indiğini doğruluyor.
2. **Payda ölçülür, varsayılmaz.** AAS = aktif oturum toplamı / ALINAN örnek sayısı. Örnek
   sayısını `60/aralık` diye sabit varsaymak, örnekleyicinin gecikip az örnek aldığı
   dakikaları — yani tam da sıkıntının yaşandığı anları — olduğundan sakin gösterirdi.

Ayrıca beklemenin YOKLUĞUNUN da veri olduğu (`wait_event_type IS NULL` = CPU) burada
kilitleniyor: bu satırları atmak yükün en büyük parçasını görünmez yapardı.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.collectors.base import SamplingConnection
from app.database import SessionLocal, init_db
from app.domain.waits import (
    CATEGORY_ORDER,
    WaitCategory,
    classify_postgres_wait,
    classify_sqlserver_wait,
    is_load_bearing,
)
from app.models import ActiveSessionMinute, Instance, WaitQuerySignature, WaitSampleMinute
from app.services import wait_sampling
from app.services.credentials import encrypt_secret


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    wait_sampling.reset_state()
    yield
    wait_sampling.reset_state()


async def _instance(**over) -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"wait-{uuid.uuid4().hex[:8]}",
            engine=over.pop("engine", "postgresql"),
            host="h", port=5432, database="d", username="u",
            password=encrypt_secret("x"),
            **over,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


class FakeCollector:
    """Sabit bir fotoğraf dizisi döndüren collector — gerçek bir veritabanı olmadan
    örnekleyicinin muhasebesini test etmek için."""

    def __init__(self, snapshots: list[dict]) -> None:
        self.snapshots = snapshots
        self.calls = 0
        self.opened = 0
        self.closed = 0

    async def open_sampling_connection(self):
        self.opened += 1
        return SamplingConnection(raw=self, capabilities={"has_query_id": True})

    async def sample_active_sessions(self, conn):
        snapshot = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot

    async def close(self):
        self.closed += 1


def _snapshot(*sessions: tuple[str, str, str]) -> dict:
    """(queryid, kategori, olay) üçlülerinden fotoğraf üretir."""
    return {
        "sessions": [
            {"queryid": q, "query": f"SELECT {q}", "wait_category": c, "wait_event": e, "blocked": c == "lock"}
            for q, c, e in sessions
        ],
        "blocked": sum(1 for _, c, _ in sessions if c == "lock"),
        "has_query_id": True,
    }


async def _run_ticks(instance: Instance, collector: FakeCollector, moments: list[datetime]) -> None:
    wait_sampling._samplers[instance.id] = wait_sampling._InstanceSampler(
        instance_id=instance.id,
        engine=wait_sampling.DatabaseEngine.POSTGRESQL,
        collector=collector,  # type: ignore[arg-type]
    )
    for moment in moments:
        await wait_sampling._sample_instance(instance, moment)
    async with SessionLocal() as session:
        await wait_sampling.flush_all_buckets(session)
        await session.commit()


# --- Taksonomi ---------------------------------------------------------------------------


def test_absence_of_a_wait_means_cpu_not_missing_data():
    """PostgreSQL'de aktif ama beklemeyen oturum CPU'da çalışıyordur.

    Bunu "veri yok" sayıp atmak, veritabanı yükünün genellikle EN BÜYÜK bileşenini
    görünmez yapar ve grafiği "sistem hep bekliyor" diye yalancı hale getirir.
    """
    assert classify_postgres_wait(None) == WaitCategory.CPU
    assert classify_postgres_wait("") == WaitCategory.CPU
    assert classify_sqlserver_wait(None) == WaitCategory.CPU


def test_postgres_wait_types_map_to_shared_categories():
    assert classify_postgres_wait("IO") == WaitCategory.IO
    assert classify_postgres_wait("Lock") == WaitCategory.LOCK
    assert classify_postgres_wait("LWLock") == WaitCategory.LWLOCK
    assert classify_postgres_wait("Client") == WaitCategory.CLIENT
    assert classify_postgres_wait("IPC") == WaitCategory.IPC
    # Bilinmeyen bir tip sessizce CPU'ya düşmemeli — CPU "iş yapıyor" demek, bilinmeyen değil.
    assert classify_postgres_wait("SomethingNew") == WaitCategory.OTHER


def test_sqlserver_scheduler_yield_is_cpu_pressure_not_a_wait():
    """SOS_SCHEDULER_YIELD bir "bekleme" gibi görünür ama anlamı "CPU kotasını doldurdu,
    sıraya girdi" — kaynağı işlemcidir. IO ya da kilit saymak, CPU sorununu tamamen yanlış
    yerde arattırır."""
    assert classify_sqlserver_wait("SOS_SCHEDULER_YIELD") == WaitCategory.CPU


def test_sqlserver_prefix_map_distinguishes_page_io_latch_from_page_latch():
    """PAGEIOLATCH_ (diskten sayfa okuma) ile PAGELATCH_ (bellekteki sayfa için kısa latch)
    tek harf farkla bambaşka teşhislerdir: biri disk, diğeri iç çekişme."""
    assert classify_sqlserver_wait("PAGEIOLATCH_SH") == WaitCategory.IO
    assert classify_sqlserver_wait("PAGELATCH_EX") == WaitCategory.LWLOCK
    assert classify_sqlserver_wait("LCK_M_X") == WaitCategory.LOCK
    assert classify_sqlserver_wait("ASYNC_NETWORK_IO") == WaitCategory.CLIENT
    assert classify_sqlserver_wait("CXPACKET") == WaitCategory.IPC
    assert classify_sqlserver_wait("RESOURCE_SEMAPHORE") == WaitCategory.MEMORY


def test_every_category_has_a_label_meaning_and_a_place_in_the_order():
    """Grafik ve öneri metinleri bu üç tablodan besleniyor; yeni bir kategori eklenip
    birine yazılmazsa arayüzde ham İngilizce anahtar (`lwlock`) görünür."""
    from app.domain.waits import CATEGORY_LABELS, CATEGORY_MEANING

    for category in WaitCategory:
        assert category in CATEGORY_LABELS, category
        assert category in CATEGORY_MEANING, category
        assert category in CATEGORY_ORDER, category
    assert len(CATEGORY_ORDER) == len(set(CATEGORY_ORDER)) == len(list(WaitCategory))


def test_background_idle_waits_are_not_counted_as_load():
    assert is_load_bearing(str(WaitCategory.CPU))
    assert is_load_bearing(str(WaitCategory.LOCK))
    assert not is_load_bearing(str(WaitCategory.ACTIVITY))


# --- Dakikalık toplama -------------------------------------------------------------------


async def test_sixty_samples_in_one_minute_become_one_row_per_combination():
    """ÖRNEKLEYİCİNİN VARLIK SEBEBİ: 60 tur = 60 satır DEĞİL.

    Aynı sorgu aynı beklemede 60 kez görüldüyse veritabanına tek satır düşer, sayaç 60 olur.
    Ham saklamak orta yüklü tek bir instance'ta günde milyonlarca satır demekti.
    """
    instance = await _instance()
    collector = FakeCollector([_snapshot(("q1", "io", "DataFileRead"))])
    base = datetime(2026, 9, 8, 10, 30, tzinfo=UTC)
    await _run_ticks(instance, collector, [base + timedelta(seconds=i) for i in range(60)])

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(WaitSampleMinute).where(WaitSampleMinute.instance_id == instance.id)
            )
        ).scalars().all()
    assert len(rows) == 1, f"60 örnek {len(rows)} satır oldu — dakikalık toplama çalışmıyor"
    assert rows[0].sample_count == 60
    assert rows[0].wait_category == "io"
    assert rows[0].wait_event == "DataFileRead"


async def test_samples_taken_is_measured_not_assumed():
    """Payda ÖLÇÜLÜYOR. Bir dakikada 60 değil 12 tur çalıştıysa payda 12'dir.

    Sabit `60/aralık` varsaymak, örnekleyicinin geciktiği ya da sunucunun erişilemediği
    dakikaları beşte bir yüke sahipmiş gibi gösterirdi — yani tam da incelenmesi gereken
    dakikaları sakinleştirirdi.
    """
    instance = await _instance()
    collector = FakeCollector([_snapshot(("q1", "cpu", ""), ("q2", "lock", "relation"))])
    base = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
    await _run_ticks(instance, collector, [base + timedelta(seconds=i) for i in range(12)])

    async with SessionLocal() as session:
        totals = (
            await session.execute(
                select(ActiveSessionMinute).where(ActiveSessionMinute.instance_id == instance.id)
            )
        ).scalar_one()
    assert totals.samples_taken == 12
    assert totals.active_sessions_sampled == 24  # 12 tur × 2 oturum
    assert totals.blocked_sessions_sampled == 12  # her turda bir kilitli oturum


async def test_minute_rollover_closes_the_previous_bucket():
    instance = await _instance()
    collector = FakeCollector([_snapshot(("q1", "cpu", ""))])
    base = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    moments = [base + timedelta(seconds=i) for i in range(30)]
    moments += [base + timedelta(minutes=1, seconds=i) for i in range(30)]
    await _run_ticks(instance, collector, moments)

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ActiveSessionMinute)
                .where(ActiveSessionMinute.instance_id == instance.id)
                .order_by(ActiveSessionMinute.minute)
            )
        ).scalars().all()
    assert [r.minute.replace(tzinfo=UTC) for r in rows] == [base, base + timedelta(minutes=1)]
    assert [r.samples_taken for r in rows] == [30, 30]


async def test_restart_within_the_same_minute_adds_instead_of_overwriting():
    """Worker dakika ortasında yeniden başlarsa aynı dakika için ikinci bir kova yazılır.

    Üzerine yazsaydık ilk yarı sessizce kaybolurdu ve o dakika olduğundan sakin görünürdü.
    """
    instance = await _instance()
    base = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)

    collector = FakeCollector([_snapshot(("q1", "io", "DataFileRead"))])
    await _run_ticks(instance, collector, [base + timedelta(seconds=i) for i in range(20)])
    wait_sampling.reset_state()
    collector2 = FakeCollector([_snapshot(("q1", "io", "DataFileRead"))])
    await _run_ticks(instance, collector2, [base + timedelta(seconds=30 + i) for i in range(10)])

    async with SessionLocal() as session:
        totals = (
            await session.execute(
                select(ActiveSessionMinute).where(ActiveSessionMinute.instance_id == instance.id)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(WaitSampleMinute).where(WaitSampleMinute.instance_id == instance.id)
            )
        ).scalars().all()
    assert totals.samples_taken == 30, "ikinci kova ilkinin üzerine yazmış"
    assert len(rows) == 1 and rows[0].sample_count == 30


async def test_query_text_is_stored_once_in_a_dictionary_not_per_row():
    instance = await _instance()
    collector = FakeCollector([_snapshot(("q1", "cpu", ""), ("q2", "io", "DataFileRead"))])
    base = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    await _run_ticks(instance, collector, [base + timedelta(seconds=i) for i in range(40)])

    async with SessionLocal() as session:
        signatures = (
            await session.execute(
                select(WaitQuerySignature).where(WaitQuerySignature.instance_id == instance.id)
            )
        ).scalars().all()
    assert {s.queryid for s in signatures} == {"q1", "q2"}
    assert {s.query_text for s in signatures} == {"SELECT q1", "SELECT q2"}


async def test_a_failing_target_drops_the_connection_and_does_not_lose_earlier_samples():
    """Kalıcı bağlantı, kopmuş bağlantıyı sonsuza kadar taşımak demek değil: hata anında
    bağlantı düşürülür ve bir sonraki turda yeniden kurulur. O ana kadar toplanan örnekler
    kaybolmaz."""
    instance = await _instance()
    good = _snapshot(("q1", "cpu", ""))
    collector = FakeCollector([good, good, RuntimeError("bağlantı koptu"), good])
    base = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    await _run_ticks(instance, collector, [base + timedelta(seconds=i) for i in range(4)])

    async with SessionLocal() as session:
        totals = (
            await session.execute(
                select(ActiveSessionMinute).where(ActiveSessionMinute.instance_id == instance.id)
            )
        ).scalar_one()
    assert totals.samples_taken == 3, "hatalı tur sayılmamalı ama diğerleri korunmalı"
    assert collector.opened == 2, "hatadan sonra bağlantı yeniden kurulmalı"


async def test_mongodb_instances_are_not_sampled():
    """MongoDB'de karşılığı olan bir bekleme sözlüğü yok; sessizce boş veri üretmek yerine
    hiç örneklenmiyor."""
    async with SessionLocal() as session:
        mongo = Instance(
            name=f"mongo-{uuid.uuid4().hex[:8]}", engine="mongodb", host="h", port=27017,
            database="d", username="u", password=encrypt_secret("x"),
        )
        session.add(mongo)
        await session.commit()
        loaded = await wait_sampling._load_instances(session, datetime.now(UTC))
    assert all(i.engine != "mongodb" for i in loaded)


async def test_disabled_instances_are_not_sampled():
    instance = await _instance(enabled=False)
    async with SessionLocal() as session:
        loaded = await wait_sampling._load_instances(session, datetime.now(UTC))
    assert instance.id not in {i.id for i in loaded}
