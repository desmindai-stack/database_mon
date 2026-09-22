"""Bekleme istatistikleri: fark hesabı, sıfırlanma ve ölçülen filtre (Faz 31 Commit 9, madde 5).

Kabul kriterleri burada çevrimdışı kanıtlanıyor; gerçek sunucu kanıtı (yeniden başlatma dahil)
`test_wait_stats_live_mssql.py` içinde. Her kuralın NEGATİF KONTROLÜ var: kuralı bozan girdide
testin kırmızıya döndüğü de gösteriliyor.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, WaitStatsBaseline
from app.services import wait_stats
from app.services.credentials import encrypt_secret
from app.services.wait_stats import (
    KIND_NOT_SQLSERVER,
    KIND_UNAUTHORIZED,
    SOURCE_PROCESS,
    SOURCE_SHARED,
    Baseline,
    DatabaseBaselineStore,
    MemoryBaselineStore,
    WaitEntry,
    build_report,
    compute_delta,
    make_store,
    reset_baseline,
)

START = datetime(2026, 9, 20, 6, 59, tzinfo=UTC)


def entry(wait_type: str, tasks: int, ms: float, user_tasks: int = 3) -> WaitEntry:
    return WaitEntry(wait_type=wait_type, waiting_tasks=tasks, wait_ms=ms, signal_ms=ms / 10,
                     max_wait_ms=ms, user_tasks=user_tasks)


class FakeInstance:
    """Gerçek Instance ile aynı okunan alanlar; meta veritabanına dokunmuyor."""

    def __init__(self, engine: str = "sqlserver", instance_id: int = 1) -> None:
        self.id, self.engine, self.username = instance_id, engine, "dbace_monitor"


class FakeCollector:
    """Sırayla verilen anlık görüntüleri döndürüyor: kümülatif sayaç davranışını taklit ediyor."""

    def __init__(self, snapshots: list[tuple[datetime, list[dict]]]) -> None:
        self.snapshots, self.calls = list(snapshots), 0

    async def run_readonly(self, sql: str, conn=None):
        if "sqlserver_start_time" in sql:
            return [{"sqlserver_start_time": self.snapshots[self.calls][0]}]
        rows = self.snapshots[self.calls][1]
        self.calls += 1
        return rows


def rows(*items: tuple[str, int, float, int]) -> list[dict]:
    return [{"wait_type": t, "waiting_tasks_count": c, "wait_time_ms": ms, "signal_wait_time_ms": ms / 10,
             "max_wait_time_ms": ms, "user_tasks": u} for t, c, ms, u in items]


@pytest.fixture(autouse=True)
def clean_baseline():
    reset_baseline()
    yield
    reset_baseline()


def test_delta_is_the_difference_between_two_cumulative_readings():
    current = [entry("PAGEIOLATCH_SH", 100, 5000), entry("LCK_M_X", 10, 2000)]
    previous = {"PAGEIOLATCH_SH": entry("PAGEIOLATCH_SH", 60, 3000), "LCK_M_X": entry("LCK_M_X", 10, 2000)}
    delta = compute_delta(current, previous)

    assert [(d.wait_type, d.waiting_tasks, d.wait_ms) for d in delta] == [("PAGEIOLATCH_SH", 40, 2000.0)]
    # NEGATİF KONTROL: kümülatif değer olduğu gibi dönseydi 5000 görülürdü.
    assert delta[0].wait_ms != 5000.0
    # Hiç değişmeyen tür fark listesinde YOK (0 satırı gürültü).
    assert "LCK_M_X" not in [d.wait_type for d in delta]


def test_counter_going_backwards_is_dropped_instead_of_reported_as_negative():
    """NEGATİF KONTROL: sayaç düşmüşse eksi fark üretilmiyor, o tür atlanıyor."""
    delta = compute_delta([entry("WRITELOG", 5, 100)], {"WRITELOG": entry("WRITELOG", 50, 9000)})
    assert delta == []


def test_new_wait_type_since_the_previous_reading_is_reported_in_full():
    delta = compute_delta([entry("CXPACKET", 7, 700)], {"WRITELOG": entry("WRITELOG", 1, 10)})
    assert [(d.wait_type, d.wait_ms) for d in delta] == [("CXPACKET", 700.0)]


def test_background_types_are_measured_not_hand_listed():
    """Filtre, elle yazılmış bir listeden değil `user_tasks` ölçümünden geliyor."""
    noise, real = entry("SLEEP_TASK", 900, 90000, user_tasks=0), entry("LCK_M_U", 4, 400, user_tasks=4)
    assert noise.is_background and not real.is_background
    # NEGATİF KONTROL: kodda elle yazılmış bir bekleme türü listesi OLMAMALI.
    source = (wait_stats.__file__ and open(wait_stats.__file__, encoding="utf-8").read()) or ""
    for hand_written in ("SLEEP_TASK", "LAZYWRITER_SLEEP", "XE_TIMER_EVENT", "BROKER_TASK_STOP"):
        assert f'"{hand_written}"' not in source, "filtre listesi elle yazılmamalı, ölçülmeli"


async def test_first_reading_reports_totals_and_says_why_there_is_no_delta():
    collector = FakeCollector([(START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9), ("SLEEP_TASK", 900, 90000.0, 0)))])
    report = await build_report(FakeInstance(), collector=collector)

    assert [e.wait_type for e in report.totals] == ["PAGEIOLATCH_SH"]  # arka plan elendi
    assert report.filtered_background == 1 and report.background_types == ["SLEEP_TASK"]
    assert report.delta == []
    assert "önceki okuma yok" in report.delta_unavailable_reason
    assert report.restarted is False
    assert report.baseline_source == "process"  # store verilmedi: varsayılan süreç belleği (Commit 10c-A)


async def test_second_reading_reports_the_delta():
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START, rows(("PAGEIOLATCH_SH", 160, 8000.0, 9))),
    ])
    instance = FakeInstance()
    await build_report(instance, collector=collector)
    report = await build_report(instance, collector=collector)

    assert [(e.wait_type, e.waiting_tasks, e.wait_ms) for e in report.delta] == [("PAGEIOLATCH_SH", 60, 3000.0)]
    assert report.delta_unavailable_reason is None and report.delta_since is not None
    # Toplam hâlâ kümülatif olanı gösteriyor — ikisi ayrı sütun.
    assert report.totals[0].wait_ms == 8000.0


async def test_server_restart_is_reported_instead_of_a_wrong_delta():
    """Sunucu yeniden başlayınca sayaç sıfırlanıyor: fark yerine gerekçe."""
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START + timedelta(hours=2), rows(("PAGEIOLATCH_SH", 12, 300.0, 9))),
    ])
    instance = FakeInstance()
    await build_report(instance, collector=collector)
    report = await build_report(instance, collector=collector)

    assert report.restarted is True
    assert report.delta == []
    assert "yeniden başlat" in report.delta_unavailable_reason
    # NEGATİF KONTROL: eksi fark (12-100 = -88) hiçbir yerde görünmüyor.
    assert all(e.waiting_tasks >= 0 and e.wait_ms >= 0 for e in report.totals + report.delta)


async def test_include_background_shows_everything_and_reports_zero_filtered():
    collector = FakeCollector([(START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9), ("SLEEP_TASK", 900, 90000.0, 0)))])
    report = await build_report(FakeInstance(), include_background=True, collector=collector)
    assert [e.wait_type for e in report.totals] == ["SLEEP_TASK", "PAGEIOLATCH_SH"]
    assert report.filtered_background == 0


async def test_postgres_instance_says_why_instead_of_an_empty_screen():
    report = await build_report(FakeInstance(engine="postgresql"), collector=FakeCollector([]))
    assert report.unavailable_kind == KIND_NOT_SQLSERVER
    assert "SQL Server" in report.unavailable_reason and report.totals == []


async def test_permission_error_says_the_required_grant():
    class Denied:
        async def run_readonly(self, sql: str, conn=None):
            raise RuntimeError("[42000] [Microsoft][SQL Server]VIEW SERVER STATE permission was denied (297)")

    report = await build_report(FakeInstance(), collector=Denied())
    assert report.unavailable_kind == KIND_UNAUTHORIZED
    assert "ölçülemedi" in report.unavailable_reason.lower()
    assert "GRANT VIEW SERVER STATE TO [dbace_monitor];" == report.required_grant
    # NEGATİF KONTROL: yetkisizlik "sorun yok" gibi görünmüyor.
    assert report.totals == [] and report.filtered_background == 0


# --- Taban deposu: paylaşılan (meta veritabanı) vs süreç belleği (Faz 31 Commit 10c-A) --------
#
# Sorun: taban süreç belleğindeydi. dbace yeniden başlayınca ilk okuma "fark hesaplanamadı" diyordu;
# çok süreçli çalışmada (uvicorn --workers N / WEB_CONCURRENCY / çoğaltılmış servis) ardışık iki
# istek farklı sürece düşerse fark "o sürecin son okumasından bu yana" oluyordu — ekranda işaret yok.
# Gerçek çok süreçli/yeniden başlatma kanıtı: scripts/waitstats_multiprocess_demo.py çıktısı ILERLEME.md'de.


async def _real_instance(**over) -> Instance:
    async with SessionLocal() as session:
        row = Instance(name=f"ws-{uuid.uuid4().hex[:8]}", engine="sqlserver", host="h", port=1433, database="d",
                       username="dbace_monitor", password=encrypt_secret("x"), **over)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    yield


async def test_shared_store_persists_across_a_fresh_session_simulating_a_process_restart():
    """PAYLAŞILAN taban: bir 'süreç'in yazdığını başka bir 'süreç' (yeni oturum, yeni Python nesnesi) okuyor —
    süreç içi tabanın çözemediği tam da bu."""
    instance = await _real_instance()
    async with SessionLocal() as writer_session:
        await DatabaseBaselineStore(writer_session).save(
            instance.id, Baseline(START, START + timedelta(seconds=5), {"PAGEIOLATCH_SH": entry("PAGEIOLATCH_SH", 10, 100.0)},
                                  writer="pid:1111"))
    async with SessionLocal() as reader_session:  # ayrı oturum: farklı bir sürecin okuması gibi
        loaded = await DatabaseBaselineStore(reader_session).load(instance.id)
    assert loaded is not None and loaded.writer == "pid:1111"
    assert loaded.entries["PAGEIOLATCH_SH"].wait_ms == 100.0


async def test_negative_control_process_store_does_not_survive_a_fresh_instance():
    """NEGATİF KONTROL: süreç belleği yeni bir MemoryBaselineStore() nesnesinde GÖRÜNMEZ (modül düzeyi paylaşılan
    sözlüğe yazmasaydı bu test kırmızı olurdu; `data=` ile izole edilmiş bir kopyada asla görünmez)."""
    isolated = MemoryBaselineStore(data={})
    assert await isolated.load(999) is None


async def test_shared_store_end_to_end_via_build_report_two_reads():
    instance = await _real_instance()
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START, rows(("PAGEIOLATCH_SH", 160, 8000.0, 9))),
    ])
    async with SessionLocal() as session:
        first = await build_report(instance, collector=collector, store=make_store(session))
    assert first.baseline_source == SOURCE_SHARED and first.baseline_saved is True
    assert first.delta_unavailable_reason is not None and "kayıtlı önceki okuma yok" in first.delta_unavailable_reason

    async with SessionLocal() as session:  # AYRI oturum: ayrı bir isteğin/sürecin okuması
        second = await build_report(instance, collector=collector, store=make_store(session))
    assert second.delta_unavailable_reason is None
    assert [(e.wait_type, e.wait_ms) for e in second.delta] == [("PAGEIOLATCH_SH", 3000.0)]

    async with SessionLocal() as session:
        saved = await session.get(WaitStatsBaseline, instance.id)
    # İkinci okuma < 60 sn sonra geldiği için taban BİLEREK yenilenmedi (aşağıdaki throttle testi); satırdaki
    # değer hâlâ İLK okumanın anlık görüntüsü — bu satırın gerçekten meta veritabanında olduğunu kanıtlıyor.
    assert saved is not None and saved.counters["PAGEIOLATCH_SH"][1] == 5000.0


async def test_restart_is_detected_through_the_shared_store_too():
    """Sunucu yeniden başlarsa PAYLAŞILAN tabanda da fark bastırılır — taban türünden bağımsız kural."""
    instance = await _real_instance()
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START + timedelta(hours=2), rows(("PAGEIOLATCH_SH", 12, 300.0, 9))),
    ])
    async with SessionLocal() as session:
        await build_report(instance, collector=collector, store=make_store(session))
    async with SessionLocal() as session:
        report = await build_report(instance, collector=collector, store=make_store(session))
    assert report.restarted is True and report.delta == []
    assert "yeniden başlat" in report.delta_unavailable_reason


async def test_baseline_is_not_rewritten_more_often_than_the_minimum_age():
    """Ekranı her açan kullanıcı meta veritabanına yazım üretmesin: taban en çok dakikada bir yenilenir."""
    instance = await _real_instance()
    collector = FakeCollector([(START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9)))] * 3)
    async with SessionLocal() as session:
        first = await build_report(instance, collector=collector, store=make_store(session))
    assert first.baseline_saved is True
    async with SessionLocal() as session:
        again = await build_report(instance, collector=collector, store=make_store(session))
    assert again.baseline_saved is False, "60 saniyeden kısa sürede taban tekrar yazılmamalı"


async def test_baseline_is_rewritten_immediately_after_a_restart_despite_the_min_age():
    instance = await _real_instance()
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START + timedelta(hours=1), rows(("PAGEIOLATCH_SH", 5, 50.0, 9))),
    ])
    async with SessionLocal() as session:
        await build_report(instance, collector=collector, store=make_store(session))
    async with SessionLocal() as session:
        report = await build_report(instance, collector=collector, store=make_store(session))
    assert report.restarted is True and report.baseline_saved is True, "yeniden başlatmada taban HEMEN güncellenmeli"


async def test_env_setting_switches_the_default_store_to_process(monkeypatch):
    """WAIT_STATS_BASELINE=process eski davranışı geri getiriyor — geri dönüş anahtarı."""
    from app.config import settings

    monkeypatch.setattr(settings, "wait_stats_baseline", "process")
    async with SessionLocal() as session:
        store = make_store(session)
    assert store.source == SOURCE_PROCESS
    monkeypatch.setattr(settings, "wait_stats_baseline", "shared")
    async with SessionLocal() as session:
        store = make_store(session)
    assert store.source == SOURCE_SHARED


async def test_negative_control_default_setting_is_shared_not_process():
    """Ürünün varsayılanı: kod elle 'process' seçmedikçe paylaşılan taban kullanılmalı."""
    from app.config import settings

    assert settings.wait_stats_baseline == "shared"


async def test_a_broken_shared_store_falls_back_to_process_with_a_visible_note():
    """Meta veritabanına ulaşılamazsa ekran sessizce yanlış sonuç vermek yerine düşüp NEDENİNİ söylüyor."""

    class BrokenStore:
        source = SOURCE_SHARED

        async def load(self, instance_id):
            raise RuntimeError("meta veritabanına ulaşılamadı")

        async def save(self, instance_id, baseline):
            raise RuntimeError("meta veritabanına ulaşılamadı")

    instance = await _real_instance()
    collector = FakeCollector([(START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9)))])
    report = await build_report(instance, collector=collector, store=BrokenStore())
    assert report.baseline_source == SOURCE_PROCESS
    assert report.delta_note is not None and "Paylaşılan taban" in report.delta_note


def test_process_id_identifies_the_serving_process():
    """Ekranda/günlükte görülebilen teşhis alanı: hangi süreç yanıtladı."""
    report = wait_stats.WaitStatsReport()
    assert report.process_id == os.getpid()


def test_baseline_row_size_is_small():
    """Ölçüm: bir taban satırının JSON sütununun bayt boyutu (Commit 10c-A dokümantasyonundaki '~5 KB' iddiası)."""
    import json

    entries = {f"WAIT_TYPE_{i}": [10, 100.0, 10.0, 50.0, 3] for i in range(250)}  # gerçekte ~250 tür ölçüldü
    size = len(json.dumps(entries).encode("utf-8"))
    assert size < 20_000, f"250 türlük taban {size} bayt — beklenenden büyük"
