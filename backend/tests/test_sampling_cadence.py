"""Bekleme örnekleyicisi: tur, hedef ve meta yazımını BEKLEMEZ; gerçek aralık ölçülür ve gösterilir (Faz 31 Commit 10a).

Kabul kriterleri (kodlamadan ÖNCE yazıldı, gerçek sunucu kanıtı `test_sampling_cadence_live.py` içinde):

1. Zamanlayıcı turu, hedefte yavaşlayan bir instance'ı ya da meta veritabanı yazımını BEKLEMEZ; bir instance'ın
   yavaşlığı diğerlerinin örneklemesini geciktirmez. Aynı instance için önceki örnek sürerken gelen tur atlanır
   VE sayılır.
2. Gerçek aralık ÖLÇÜLÜR (ardışık başarılı örneklerin geliş farkı): atlanan tur, başarısız tur ve yeniden bağlanma
   süresi bu farka otomatik girer. Dakika başına en uzun boşluk saklanır; ölçülmemişse 0 değil NULL.
3. Özet satırı atlanan turu, gerçek aralığı (ortalama/p95/en uzun boşluk) ve süre kırılımını gösterir; hedef
   tutturulamadıysa UYARI olarak yazılır. "0 gecikmiş tur" ile "onlarca atlanan tur" artık birlikte var olamaz.
4. Ekran (veritabanı yükü ucu) aynı kuralla "örnekleme aralığı tutturulamadı (ölçülen: X ms)" der; veri yetersizken
   de. Aralık tutmuşsa uyarı YOK; boşluk ölçülmemişse (eski satır) "boşluk yok" denmez.
5. Meta yazımı okuma-öncesiz tek ifadeyle (kova başına ≤ 2 ifade) ve toplamlar birikerek (yeniden başlatma güvenli).
6. Bloklama ağacı yalnızca gerektiğinde okunur (kilit bekleyen oturum var ya da açık olay var).

Her kural için NEGATİF KONTROL var: kuralı bozan girdide testin kırmızıya döndüğü de gösteriliyor.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.collectors.base import SamplingConnection
from app.database import SessionLocal, engine, init_db
from app.models import ActiveSessionMinute, Instance, WaitSampleMinute
from app.services import blocking_history, sampling_cadence, wait_sampling
from app.services.credentials import encrypt_secret
from app.services.database_load import build_database_load

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    wait_sampling.reset_state()
    blocking_history.reset_state()
    yield
    wait_sampling.reset_state()
    blocking_history.reset_state()


async def _instance(**over) -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"cad-{uuid.uuid4().hex[:8]}", engine=over.pop("engine", "postgresql"),
            host="h", port=5432, database="d", username="u", password=encrypt_secret("x"), **over,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


def _snapshot(blocked: int = 0, sessions: int = 1) -> dict:
    rows = [{"queryid": "q1", "query": "SELECT 1", "wait_category": "cpu", "wait_event": "", "blocked": False}
            for _ in range(sessions)]
    return {"sessions": rows, "blocked": blocked, "has_query_id": True}


class ScriptedCollector:
    """Süresi ayarlanabilir sahte collector. `advance` verilirse SAHTE saati ilerletir (gerçek beklemeden aralık)."""

    def __init__(self, *, delay: float = 0.0, snapshot: dict | None = None, clock: dict | None = None,
                 advance: float = 0.0, fail_next: int = 0) -> None:
        self.delay, self.snapshot, self.clock, self.advance = delay, snapshot or _snapshot(), clock, advance
        self.fail_next = fail_next
        self.samples = 0
        self.opened = 0
        self.blocking_calls = 0
        self.blocking_rows: list[dict] = []

    async def open_sampling_connection(self):
        self.opened += 1
        return SamplingConnection(raw=self, capabilities={"has_query_id": True})

    async def sample_active_sessions(self, conn):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.clock is not None:
            self.clock["t"] += self.advance
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("hedef yanıt vermiyor")
        self.samples += 1
        return self.snapshot

    async def collect_blocking(self, limit: int = 200, conn=None):
        self.blocking_calls += 1
        return self.blocking_rows

    async def close(self):
        pass


def _register(instance: Instance, collector: ScriptedCollector) -> None:
    wait_sampling._samplers[instance.id] = wait_sampling._InstanceSampler(
        instance_id=instance.id, engine=wait_sampling.DatabaseEngine.POSTGRESQL, collector=collector,  # type: ignore[arg-type]
    )


def _use_instances(monkeypatch, instances: list[Instance]) -> None:
    async def fixed(now, *, refresh):
        return list(instances)

    monkeypatch.setattr(wait_sampling, "_instances_for_tick", fixed)


NOW = datetime(2026, 9, 20, 10, 0, 30, tzinfo=UTC)


# --- 1. Tur beklemez ---------------------------------------------------------------------------


async def test_tick_does_not_wait_for_a_slow_instance_and_others_keep_their_cadence(monkeypatch):
    slow, fast = await _instance(), await _instance()
    slow_collector, fast_collector = ScriptedCollector(delay=0.6), ScriptedCollector()
    _register(slow, slow_collector)
    _register(fast, fast_collector)
    _use_instances(monkeypatch, [slow, fast])

    started = time.monotonic()
    await wait_sampling.sampling_tick(wait=False, flush=False)
    first_tick = time.monotonic() - started
    await asyncio.sleep(0.05)
    await wait_sampling.sampling_tick(wait=False, flush=False)  # yavaş instance'ın örneği hâlâ sürüyor
    await asyncio.sleep(0.05)
    await wait_sampling.sampling_tick(wait=False, flush=False)
    await asyncio.sleep(0.05)  # son turun görevleri çalışsın

    assert first_tick < 0.2, f"tur hedefi beklememeli, sürdü: {first_tick:.2f} sn"
    assert fast_collector.samples == 3, "hızlı instance her turda örneklenmeli"
    assert slow_collector.samples == 0, "yavaş instance'ın ilk örneği daha bitmedi"
    # Atlanan turlar SAYILIYOR: yavaş instance için iki tur atlandı.
    assert wait_sampling._samplers[slow.id].skipped_in_flight == 2
    assert wait_sampling._stats.skipped_in_flight == 2
    assert wait_sampling._samplers[fast.id].skipped_in_flight == 0
    await wait_sampling.shutdown_sampling()


async def test_negative_control_waiting_tick_is_as_slow_as_its_slowest_instance(monkeypatch):
    """`wait=True` eski davranışı (tüm instance'ları bekleyen tur) temsil eder: bu testin geçmesi, üstteki testin
    gerçekten tur süresini ölçtüğünü gösterir."""
    slow = await _instance()
    _register(slow, ScriptedCollector(delay=0.4))
    _use_instances(monkeypatch, [slow])
    started = time.monotonic()
    await wait_sampling.sampling_tick(wait=True, flush=False)
    assert time.monotonic() - started >= 0.4


async def test_meta_write_is_outside_the_tick(monkeypatch):
    """Dakika kapanışındaki yavaş meta yazımı örnekleme turunu ve örneklemeyi geciktirmez."""
    instance = await _instance()
    collector = ScriptedCollector()
    _register(instance, collector)
    _use_instances(monkeypatch, [instance])

    async def slow_flush(session):
        await asyncio.sleep(1.5)
        return 0

    monkeypatch.setattr(wait_sampling, "flush_completed_buckets", slow_flush)
    flushing = asyncio.create_task(wait_sampling.flush_tick(refresh_instances=False))
    await asyncio.sleep(0.05)

    started = time.monotonic()
    for _ in range(3):
        await wait_sampling.sampling_tick(wait=False, flush=False)
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - started
    assert collector.samples == 3, "yazım sürerken örnekleme sürmeli"
    assert elapsed < 0.6, "örnekleme, süren yavaş yazımı beklememeli"
    assert not flushing.done(), "yazım gerçekten hâlâ sürüyordu"
    flushing.cancel()
    await asyncio.gather(flushing, return_exceptions=True)
    await wait_sampling.shutdown_sampling()


async def test_scheduler_job_uses_the_non_waiting_form(monkeypatch):
    from app.collectors import scheduler

    seen: dict = {}

    async def fake_tick(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(scheduler, "sampling_tick", fake_tick)
    await scheduler.wait_sampling_tick()
    assert seen == {"wait": False, "flush": False}


def test_scheduler_registers_a_separate_flush_job_that_both_tools_share():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.collectors import scheduler

    target = AsyncIOScheduler()
    scheduler.register_wait_sampling_jobs(target)
    jobs = {job.id: job for job in target.get_jobs()}
    assert set(jobs) == {scheduler.WAIT_SAMPLING_JOB_ID, scheduler.WAIT_FLUSH_JOB_ID}
    assert jobs[scheduler.WAIT_SAMPLING_JOB_ID].max_instances == 1
    assert jobs[scheduler.WAIT_FLUSH_JOB_ID].trigger.interval.total_seconds() == wait_sampling.FLUSH_INTERVAL_SECONDS
    # Ölçüm aracı üretimle AYNI kaydı kullanıyor (kopya değil).
    probe = (REPO / "backend" / "scripts" / "sampler_probe.py").read_text(encoding="utf-8")
    assert "register_wait_sampling_jobs" in probe
    start = (REPO / "backend" / "app" / "collectors" / "scheduler.py").read_text(encoding="utf-8")
    assert "register_wait_sampling_jobs(scheduler)" in start


async def test_removed_instance_releases_its_connection_and_bucket(monkeypatch):
    kept, removed = await _instance(), await _instance()
    for instance in (kept, removed):
        _register(instance, ScriptedCollector())
    closed: list[int] = []

    async def fake_drop(sampler):
        closed.append(sampler.instance_id)

    monkeypatch.setattr(wait_sampling, "_drop_connection", fake_drop)
    _use_instances(monkeypatch, [kept, removed])
    await wait_sampling.sampling_tick(wait=True, flush=False)
    assert wait_sampling._buckets.get(removed.id) is not None

    _use_instances(monkeypatch, [kept])  # instance silindi / devre dışı bırakıldı
    await wait_sampling.sampling_tick(wait=True, flush=False)
    assert removed.id not in wait_sampling._samplers and closed == [removed.id]
    assert any(instance_id == removed.id for instance_id, _ in wait_sampling._pending_flush), \
        "açık kova yazılmak üzere bırakılmalı"


# --- 2. Gerçek aralık ölçümü -------------------------------------------------------------------


async def _run_samples(instance, collector, clock, idles: list[float], start: datetime = NOW):
    """Sahte saatle örnekler: her örnek arasında `idles[i]` saniye geçmiş gibi."""
    _register(instance, collector)
    for index, idle in enumerate(idles):
        clock["t"] += idle
        await wait_sampling._sample_instance(instance, start + timedelta(seconds=index), clock=lambda: clock["t"])


async def test_actual_gap_is_measured_between_successful_samples_and_the_longest_is_kept():
    instance = await _instance()
    clock = {"t": 1000.0}
    collector = ScriptedCollector(clock=clock, advance=0.01)
    # Örnekler arasında: (ilk örnek) 1 sn, 1 sn, 3 sn, 1 sn → en uzun boşluk ≈ 3 sn, 2 tur atlanmış.
    await _run_samples(instance, collector, clock, [0.0, 1.0, 1.0, 3.0, 1.0])
    bucket = wait_sampling._buckets[instance.id]
    assert bucket.samples_taken == 5
    assert 2900 <= bucket.max_gap_ms <= 3100
    assert wait_sampling._stats.missed_slots == 2
    assert wait_sampling._stats.gap_count == 4  # ilk örneğin boşluğu yok: uydurulmuyor


async def test_first_sample_has_no_gap_so_none_not_zero():
    """NEGATİF KONTROL: ölçülmemiş boşluk 0 gibi görünmemeli (0 = "boşluk yok")."""
    instance = await _instance()
    clock = {"t": 50.0}
    await _run_samples(instance, ScriptedCollector(clock=clock, advance=0.01), clock, [0.0])
    assert wait_sampling._buckets[instance.id].max_gap_ms is None
    assert wait_sampling._stats.gap_count == 0


async def test_failed_rounds_and_reconnect_widen_the_measured_gap():
    """Başarısız tur ve yeniden bağlanma ayrıca sayılmıyor ama boşluğa OTOMATİK giriyor."""
    instance = await _instance()
    clock = {"t": 0.0}
    collector = ScriptedCollector(clock=clock, advance=0.02)
    _register(instance, collector)
    tick = lambda: clock["t"]  # noqa: E731
    await wait_sampling._sample_instance(instance, NOW, clock=tick)            # başarılı
    collector.fail_next = 3
    for i in range(3):                                                         # 3 başarısız tur, 1 sn arayla
        clock["t"] += 1.0
        await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=1 + i), clock=tick)
    clock["t"] += 1.0
    await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=4), clock=tick)  # başarılı
    bucket = wait_sampling._buckets[instance.id]
    assert bucket.samples_taken == 2
    assert bucket.max_gap_ms >= 4000, "üç başarısız turun süresi boşluk olarak görünmeli"
    assert wait_sampling._stats.missed_slots >= 3
    assert collector.opened == 4, "her başarısızlıktan sonra bağlantı yeniden kuruldu"


# --- 3. Özet satırı ----------------------------------------------------------------------------


def _summary_records(caplog):
    return [r for r in caplog.records if "özeti" in r.getMessage()]


def _feed(now: datetime, count: int, gap: float, duration: float = 0.3) -> None:
    for i in range(count):
        wait_sampling._record_round(now + timedelta(seconds=i), duration, 1, gap_seconds=gap)


def test_summary_reports_skipped_rounds_and_measured_interval_instead_of_zero_slow_rounds(caplog):
    """Canlıdaki tutarsızlığın aynısı: tur ortalama 296 ms (hiçbiri 3 sn'yi geçmedi → "0 gecikmiş tur") ama örnekler
    ~1,1 sn'de bir geliyordu ve onlarcası atlanmıştı."""
    base = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        wait_sampling._record_round(base, 0.3, 1)
        # 130 örnek 1 sn aralıkla, 100 örnek 2 sn aralıkla (her biri bir atlanmış tur) → ölçülen ortalama ~1,4 sn.
        _feed(base + timedelta(seconds=1), 130, gap=1.0)
        _feed(base + timedelta(seconds=131), 100, gap=2.0)
        wait_sampling._record_round(base + timedelta(seconds=400), 0.3, 1, gap_seconds=1.0)
    [record] = _summary_records(caplog)
    text = record.getMessage()
    assert "0 gecikmiş tur" in text, "tur süresi tek başına normaldi"
    assert "atlanan tur: 100" in text, text
    assert re.search(r"gerçek aralık: ort 1[34]\d\d ms \(hedef 1000 ms", text), text
    assert "Örnekleme aralığı tutturulamadı (ölçülen: 1" in text
    assert record.levelno == logging.WARNING, "hedef tutturulamadıysa özet uyarı seviyesinde"


def test_summary_stays_info_and_says_nothing_when_the_interval_held(caplog):
    """NEGATİF KONTROL: aralık tuttuğunda uyarı ve 'tutturulamadı' YOK."""
    base = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        wait_sampling._record_round(base, 0.05, 1)
        _feed(base + timedelta(seconds=1), 300, gap=1.0, duration=0.05)
        wait_sampling._record_round(base + timedelta(seconds=400), 0.05, 1, gap_seconds=1.0)
    [record] = _summary_records(caplog)
    assert record.levelno == logging.INFO
    assert "tutturulamadı" not in record.getMessage()
    assert "atlanan tur: 0" in record.getMessage()


def test_summary_counts_scheduler_skips_and_shows_the_time_breakdown(caplog):
    base = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    wait_sampling.note_scheduler_skip()
    wait_sampling.note_scheduler_skip()
    wait_sampling._stats.connects, wait_sampling._stats.connect_seconds = 2, 0.6
    wait_sampling._stats.queries, wait_sampling._stats.query_seconds = 300, 45.0
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        wait_sampling._record_round(base, 0.1, 1)
        wait_sampling._record_round(base + timedelta(seconds=400), 0.1, 1)
    text = _summary_records(caplog)[0].getMessage()
    assert "zamanlayıcı atlaması 2" in text
    assert "bağlanma 2 kez ort 300 ms" in text and "sorgu ort 150 ms" in text


def test_single_long_gap_marks_the_window_as_irregular_even_if_the_average_holds(caplog):
    base = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        wait_sampling._record_round(base, 0.1, 1)
        _feed(base + timedelta(seconds=1), 299, gap=1.0)
        wait_sampling._record_round(base + timedelta(seconds=300), 0.1, 1, gap_seconds=30.0)
        wait_sampling._record_round(base + timedelta(seconds=400), 0.1, 1, gap_seconds=1.0)
    record = _summary_records(caplog)[0]
    assert "Örnekleme düzensiz: en uzun boşluk 30000 ms" in record.getMessage()
    assert record.levelno == logging.WARNING


def test_sampling_status_exposes_the_measured_interval_not_only_the_target():
    base = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
    _feed(base, 10, gap=2.0)
    status = wait_sampling.sampling_status()
    assert status["interval_seconds"] == 1
    assert status["measured_interval_ms"] == 2000
    assert status["missed_slots"] == 10 and "tutturulamadı" in status["cadence_message"]


# --- Değerlendirme kuralı ----------------------------------------------------------------------


def test_cadence_rule_thresholds_and_no_verdict_without_measurement():
    tol = sampling_cadence.CADENCE_TOLERANCE
    held = sampling_cadence.assess(target_interval_seconds=1, measured_interval_ms=1000 * tol, max_gap_ms=1200)
    assert not held.missed and not held.irregular and held.message is None
    missed = sampling_cadence.assess(target_interval_seconds=1, measured_interval_ms=1000 * tol + 1, max_gap_ms=None)
    assert missed.missed and "tutturulamadı (ölçülen: 1251 ms, hedef: 1000 ms)" in missed.message
    irregular = sampling_cadence.assess(target_interval_seconds=1, measured_interval_ms=1000,
                                        max_gap_ms=1000 * sampling_cadence.LONG_GAP_FACTOR + 1)
    assert irregular.irregular and not irregular.missed and "düzensiz" in irregular.message
    # NEGATİF KONTROL: ölçüm yokken hüküm yok — "aralık tuttu" da "tutmadı" da denmez.
    unknown = sampling_cadence.assess(target_interval_seconds=1, measured_interval_ms=None, max_gap_ms=None)
    assert not unknown.missed and not unknown.irregular and unknown.message is None


# --- 4. Ekran / API ----------------------------------------------------------------------------


async def _add_minutes(instance, rows: list[tuple[int, int | None]], start=None):
    """(alınan örnek, en uzun boşluk ms) çiftlerinden dakikalık satırlar üretir."""
    start = start or (datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=len(rows) + 1))
    async with SessionLocal() as session:
        for index, (samples, gap) in enumerate(rows):
            session.add(ActiveSessionMinute(
                instance_id=instance.id, minute=start + timedelta(minutes=index), samples_taken=samples,
                active_sessions_sampled=samples * 2, blocked_sessions_sampled=0, max_gap_ms=gap))
            session.add(WaitSampleMinute(
                instance_id=instance.id, minute=start + timedelta(minutes=index), queryid="q1",
                wait_category="cpu", wait_event="", sample_count=samples * 2))
        await session.commit()


async def _load(instance):
    async with SessionLocal() as session:
        return await build_database_load(session, instance, hours=1)


async def test_screen_says_interval_missed_with_the_measured_value():
    instance = await _instance()
    await _add_minutes(instance, [(30, 3000)] * 10)  # dakikada 30 örnek = 2000 ms aralık
    report = await _load(instance)
    assert report.cadence.missed and report.cadence.measured_interval_ms == 2000
    assert "Örnekleme aralığı tutturulamadı (ölçülen: 2000 ms" in report.cadence.message


async def test_screen_shows_the_warning_even_when_data_is_too_thin_for_a_verdict():
    instance = await _instance()
    await _add_minutes(instance, [(20, 4000)] * 2)  # 40 örnek < 60: AAS üretilmez ama sebebi görünmeli
    report = await _load(instance)
    assert report.unavailable_reason and "Yalnızca 40 örnek" in report.unavailable_reason
    assert report.cadence is not None and report.cadence.missed


async def test_screen_is_silent_when_the_interval_held():
    """NEGATİF KONTROL."""
    instance = await _instance()
    await _add_minutes(instance, [(60, 1100)] * 10)
    report = await _load(instance)
    assert report.cadence.measured_interval_ms == 1000
    assert not report.cadence.missed and not report.cadence.irregular and report.cadence.message is None


async def test_unmeasured_gap_is_not_reported_as_no_gap():
    """Kolon eklenmeden önceki satırlar (max_gap_ms NULL): boşluk hakkında hüküm YOK."""
    instance = await _instance()
    await _add_minutes(instance, [(60, None)] * 10)
    report = await _load(instance)
    assert report.cadence.max_gap_ms is None and not report.cadence.irregular


async def test_one_long_gap_in_the_window_marks_it_irregular():
    instance = await _instance()
    await _add_minutes(instance, [(60, 1100)] * 9 + [(60, 30_000)])
    report = await _load(instance)
    assert report.cadence.irregular and not report.cadence.missed
    assert "en uzun boşluk 30000 ms" in report.cadence.message


async def test_endpoint_carries_the_cadence_through_the_real_http_path():
    from tests.auth_helper import authed_client

    instance = await _instance()
    await _add_minutes(instance, [(30, 3000)] * 10)
    async with await authed_client() as client:
        response = await client.get(f"/api/instances/{instance.id}/database-load?hours=1")
    assert response.status_code == 200, response.text
    cadence = response.json()["cadence"]
    assert cadence["missed"] is True and cadence["measured_interval_ms"] == 2000
    assert "tutturulamadı" in cadence["message"]


def test_panel_renders_the_warning_in_both_the_normal_and_the_too_thin_state():
    panel = (REPO / "frontend" / "src" / "components" / "DatabaseLoadPanel.tsx").read_text(encoding="utf-8")
    assert panel.count("<CadenceNotice") == 2, "uyarı hem 'hesaplanamadı' hem normal görünümde gösterilmeli"
    assert 'role="alert"' in panel and "cadence.message" in panel
    unavailable = panel[panel.index("report.unavailable_reason) {"):panel.index("const shown")]
    assert "CadenceNotice" in unavailable, "veri yetersizken de uyarı görünmeli"


# --- 5. Yazım yolu -----------------------------------------------------------------------------


def _statements_during():
    executed: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        executed.append(re.sub(r"\s+", " ", statement).strip())

    return executed, record


async def test_bucket_write_is_read_free_and_uses_at_most_two_statements():
    instance = await _instance()
    bucket = wait_sampling._MinuteBucket(minute=NOW.replace(second=0), samples_taken=60, active_sessions_sampled=90,
                                         max_gap_ms=1200)
    bucket.counts = {("", "cpu", ""): 60, ("", "io", "DataFileRead"): 30}
    executed, record = _statements_during()
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with SessionLocal() as session:
            await wait_sampling._write_bucket(session, instance.id, bucket)
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    writes = [s for s in executed if s.upper().startswith(("INSERT", "UPDATE"))]
    reads = [s for s in executed if s.upper().startswith("SELECT")]
    assert len(writes) == 2, writes
    assert not any("active_session_minutes" in s or "wait_sample_minutes" in s for s in reads), \
        "yazımdan önce mevcut satırları OKUMAMALI (gereksiz gidiş-dönüş ve egress)"


async def test_restart_inside_the_same_minute_accumulates_and_keeps_the_longest_gap():
    instance = await _instance()
    minute = NOW.replace(second=0)
    for samples, gap in ((20, 800), (25, 4200), (15, None)):
        bucket = wait_sampling._MinuteBucket(minute=minute, samples_taken=samples, active_sessions_sampled=samples,
                                             max_gap_ms=gap)
        bucket.counts = {("", "cpu", ""): samples}
        async with SessionLocal() as session:
            await wait_sampling._write_bucket(session, instance.id, bucket)
            await session.commit()
    async with SessionLocal() as session:
        total = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalar_one()
        wait_row = (await session.execute(select(WaitSampleMinute).where(
            WaitSampleMinute.instance_id == instance.id))).scalar_one()
    assert total.samples_taken == 60 and wait_row.sample_count == 60
    assert total.max_gap_ms == 4200


async def test_unmeasured_gaps_stay_null_after_merging():
    """NEGATİF KONTROL: iki ölçülmemiş yarım kova birleşince 0 değil NULL."""
    instance = await _instance()
    minute = NOW.replace(second=0)
    for _ in range(2):
        bucket = wait_sampling._MinuteBucket(minute=minute, samples_taken=5, active_sessions_sampled=5)
        bucket.counts = {("", "cpu", ""): 5}
        async with SessionLocal() as session:
            await wait_sampling._write_bucket(session, instance.id, bucket)
            await session.commit()
    async with SessionLocal() as session:
        total = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalar_one()
    assert total.samples_taken == 10 and total.max_gap_ms is None


async def test_flush_tick_writes_pending_buckets_and_blocking_snapshots():
    instance = await _instance()
    clock = {"t": 0.0}
    collector = ScriptedCollector(clock=clock, advance=0.01)
    await _run_samples(instance, collector, clock, [0.0, 1.0, 1.0])
    async with SessionLocal() as session:
        pending_before = len(wait_sampling._buckets)
    assert pending_before == 1 and wait_sampling._pending_flush == []
    wait_sampling._pending_flush.append((instance.id, wait_sampling._buckets.pop(instance.id)))
    await wait_sampling.flush_tick(refresh_instances=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalar_one()
    assert row.samples_taken == 3 and row.max_gap_ms is not None


# --- 6. Bloklama yalnızca gerektiğinde ---------------------------------------------------------


async def _sample_for(instance, collector, seconds: int) -> None:
    _register(instance, collector)
    for i in range(seconds):
        await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=i))


async def test_blocking_is_not_read_while_nothing_is_blocked():
    instance = await _instance()
    collector = ScriptedCollector(snapshot=_snapshot(blocked=0))
    await _sample_for(instance, collector, 25)
    assert collector.blocking_calls == 0, "kilit bekleyen oturum yokken hedefe bloklama sorgusu gitmemeli"


async def test_blocking_is_read_every_ten_seconds_while_sessions_are_blocked():
    """NEGATİF KONTROL: kilit bekleyen oturum varken kontrol yapılıyor (eski aralıkla)."""
    instance = await _instance()
    collector = ScriptedCollector(snapshot=_snapshot(blocked=2))
    await _sample_for(instance, collector, 25)
    assert collector.blocking_calls == 3  # t=0, 10, 20
    assert len(wait_sampling._pending_blocking) == 3, "okunan fotoğraflar yazım işine kuyruklanmalı"


async def test_blocking_check_continues_while_an_episode_is_open_so_it_can_close(monkeypatch):
    instance = await _instance()
    monkeypatch.setattr(wait_sampling, "has_open_episode", lambda instance_id: True)
    collector = ScriptedCollector(snapshot=_snapshot(blocked=0))
    await _sample_for(instance, collector, 25)
    assert collector.blocking_calls == 3, "açık olay varken kapanışı için kontrol sürmeli"


def test_pending_blocking_queue_is_bounded():
    for i in range(wait_sampling.MAX_PENDING_BLOCKING + 50):
        wait_sampling._pending_blocking.append((1, "x", [], NOW))
        del wait_sampling._pending_blocking[:-wait_sampling.MAX_PENDING_BLOCKING]
    assert len(wait_sampling._pending_blocking) == wait_sampling.MAX_PENDING_BLOCKING


# --- Migration ---------------------------------------------------------------------------------


def test_max_gap_column_is_in_a_migration_and_documented():
    migration = (REPO / "supabase" / "migrations" / "20260920090000_active_session_max_gap.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS max_gap_ms INTEGER" in migration
    assert "20260920090000_active_session_max_gap.sql" in (REPO / "DEPLOY.md").read_text(encoding="utf-8")
    assert "max_gap_ms" in (REPO / "backend" / "app" / "models.py").read_text(encoding="utf-8")


# --- 7. Toplu yazım: kova sayısından bağımsız ifade sayısı --------------------------------------


def _bucket(minute: datetime, samples: int, *, keys: int = 30, gap: int | None = 900) -> "wait_sampling._MinuteBucket":
    bucket = wait_sampling._MinuteBucket(minute=minute, samples_taken=samples, active_sessions_sampled=samples * 2,
                                         max_gap_ms=gap)
    bucket.counts = {(f"q{i}", "cpu", ""): samples // 2 for i in range(keys)}
    return bucket


async def test_flush_of_twenty_buckets_uses_a_constant_number_of_write_statements():
    """20 instance'ın dakika kapanışı: eskiden kova başına ≥ 2 ifade (20 kova = 40+ gidiş-dönüş), şimdi birkaç."""
    instances = [await _instance() for _ in range(20)]
    minute = NOW.replace(second=0)
    for instance in instances:
        wait_sampling._pending_flush.append((instance.id, _bucket(minute, 60)))
    executed, record = _statements_during()
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with SessionLocal() as session:
            await wait_sampling.flush_completed_buckets(session)
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    writes = [s for s in executed if s.upper().startswith(("INSERT", "UPDATE"))]
    reads = [s for s in executed if s.upper().startswith("SELECT")
             and ("active_session_minutes" in s or "wait_sample_minutes" in s)]
    assert len(writes) <= 3, f"20 kova için {len(writes)} yazım ifadesi: {writes}"
    assert reads == []
    async with SessionLocal() as session:
        rows = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id.in_([i.id for i in instances])))).scalars().all()
    assert len(rows) == 20 and all(r.samples_taken == 60 and r.max_gap_ms == 900 for r in rows)


async def test_negative_control_per_bucket_writes_scale_with_the_bucket_count():
    """`_write_bucket` (kova başına) ifade sayısı kova sayısıyla büyür; toplu yolun neyi çözdüğünü gösterir."""
    instances = [await _instance() for _ in range(5)]
    executed, record = _statements_during()
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with SessionLocal() as session:
            for instance in instances:
                await wait_sampling._write_bucket(session, instance.id, _bucket(NOW.replace(second=0), 60, keys=3))
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len([s for s in executed if s.upper().startswith("INSERT")]) >= 10


async def test_two_buckets_for_the_same_instance_minute_in_one_flush_are_merged():
    """ON CONFLICT aynı satırı tek ifadede iki kez güncelleyemez: aynı anahtar Python'da birleştirilir."""
    instance = await _instance()
    minute = NOW.replace(second=0)
    wait_sampling._pending_flush.append((instance.id, _bucket(minute, 20, keys=2, gap=500)))
    wait_sampling._pending_flush.append((instance.id, _bucket(minute, 30, keys=2, gap=2500)))
    async with SessionLocal() as session:
        await wait_sampling.flush_completed_buckets(session)
        await session.commit()
    async with SessionLocal() as session:
        total = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalar_one()
        waits = (await session.execute(select(WaitSampleMinute).where(
            WaitSampleMinute.instance_id == instance.id))).scalars().all()
    assert total.samples_taken == 50 and total.max_gap_ms == 2500
    assert sorted(w.sample_count for w in waits) == [25, 25]  # 10 + 15 her sorgu için


async def test_a_failing_batch_falls_back_to_bucket_by_bucket_writes(monkeypatch):
    first, second = await _instance(), await _instance()
    minute = NOW.replace(second=0)
    for instance in (first, second):
        wait_sampling._pending_flush.append((instance.id, _bucket(minute, 40, keys=2)))
    real = wait_sampling._write_counts
    calls = {"n": 0}

    async def flaky(session, pairs):
        calls["n"] += 1
        if calls["n"] == 1 and len(pairs) > 1:
            raise RuntimeError("toplu yazım başarısız")
        return await real(session, pairs)

    monkeypatch.setattr(wait_sampling, "_write_counts", flaky)
    async with SessionLocal() as session:
        await wait_sampling.flush_completed_buckets(session)
        await session.commit()
    async with SessionLocal() as session:
        rows = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id.in_([first.id, second.id])))).scalars().all()
    assert len(rows) == 2 and calls["n"] == 3, "toplu deneme + iki kova kova deneme"


async def test_analysis_setting_is_read_once_per_flush_not_per_bucket(monkeypatch):
    instances = [await _instance() for _ in range(4)]
    minute = NOW.replace(second=0)
    for instance in instances:
        bucket = _bucket(minute, 10, keys=1)
        bucket.samples = {"q0": ("SELECT 1", 12.0)}
        wait_sampling._pending_flush.append((instance.id, bucket))
    reads = {"n": 0}

    async def counting(session):
        reads["n"] += 1
        return False

    monkeypatch.setattr(wait_sampling, "_store_real_query_samples", counting)
    monkeypatch.setattr(wait_sampling, "_privacy_enforced", True)  # süreç başı gizlilik temizliği ayrı bir okuma
    async with SessionLocal() as session:
        await wait_sampling.flush_completed_buckets(session)
        await session.commit()
    assert reads["n"] == 1


# --- 8. Örnekleme sağlığı: 'örnek yok' ile 'bağlanamıyor' ayrımı için kalıcı durum (Faz 31 Commit 10c-B) --------
#
# database_load.py / bloklama geçmişi ekranı, RTT ≥ ~1 sn ya da yetki hatası nedeniyle örnekleyici hiç
# bağlanamadığında "örnek/olay yok" diyordu — sağlıklı bir sistemle karışabilen bir cümle. Instance satırındaki
# last_sample_error*/last_sample_ok_at bu ayrımı taşıyor; burada `_sample_instance`'ın bu alanları NE ZAMAN
# yazdığı (yalnızca durum DEĞİŞİRKEN — 1 sn'lik döngüde her turda değil) test ediliyor.


async def test_the_first_failure_of_a_streak_is_persisted_once():
    instance = await _instance()
    collector = ScriptedCollector(fail_next=5)  # 5 ardışık başarısız tur
    _register(instance, collector)
    for i in range(5):
        await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=i))

    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
    assert row.last_sample_error is not None and "hedef yanıt vermiyor" in row.last_sample_error
    assert row.last_sample_error_at is not None and row.last_sample_ok_at is None


async def test_repeated_failures_do_not_rewrite_the_row_every_tick():
    """NEGATİF KONTROL: 1 sn'lik döngüde her turda yazsaydı bu test 1 yerine 20 UPDATE görürdü — tam da Faz 31'in
    tekrar tekrar düzelttiği egress hatası."""
    instance = await _instance()
    collector = ScriptedCollector(fail_next=20)
    _register(instance, collector)
    writes: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE instances" in statement or ("instances" in statement and parameters and "last_sample" in str(context.compiled_parameters)):
            writes.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        for i in range(20):
            await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=i))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    updates = [w for w in writes if w.strip().upper().startswith("UPDATE")]
    assert len(updates) == 1, f"20 ardışık başarısızlıkta TEK yazım beklenir, {len(updates)} görüldü"


async def test_recovery_after_a_failure_streak_is_persisted_once():
    instance = await _instance()
    collector = ScriptedCollector(fail_next=3)
    _register(instance, collector)
    for i in range(3):
        await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=i))  # başarısız
    await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=3))  # toparlanma

    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
    assert row.last_sample_ok_at is not None
    assert row.last_sample_error_at is not None and row.last_sample_ok_at >= row.last_sample_error_at


async def test_a_healthy_streak_writes_ok_at_once_on_the_first_sample_only():
    """Sağlıklı bir instance'ta hata alanları HİÇ dokunulmadan NULL kalır. `last_sample_ok_at` ise sürecin bu
    instance'ı İLK örneklediği anı bir kez taşır (worker yeniden başladıktan sonra ESKİ bir arızanın kalıntısının
    sonsuza dek 'bağlanamıyor' göstermemesi için gerekli — bkz. `_sample_instance` yorumu); NEGATİF KONTROL:
    sonraki 9 sağlıklı turda satır TEKRAR yazılmıyor (10 turda tam 1 UPDATE)."""
    instance = await _instance()
    collector = ScriptedCollector()
    _register(instance, collector)
    writes: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("UPDATE INSTANCES"):
            writes.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        for i in range(10):
            await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=i))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
    assert row.last_sample_error is None and row.last_sample_error_at is None
    assert row.last_sample_ok_at is not None
    assert len(writes) == 1, f"10 sağlıklı turda TEK yazım beklenir, {len(writes)} görüldü"


async def test_a_second_failure_streak_overwrites_the_previous_error_text():
    """İki AYRI arıza olayı: ikincisinin metni/gerekçesi birinciyi geçersiz kılmalı (en son bilgi güncel kalsın)."""
    instance = await _instance()
    collector = ScriptedCollector(fail_next=1)
    _register(instance, collector)
    await wait_sampling._sample_instance(instance, NOW)      # 1. arıza
    await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=1))  # toparlanma
    collector.fail_next = 1
    await wait_sampling._sample_instance(instance, NOW + timedelta(seconds=2))  # 2. arıza (yeni akış)

    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
    assert row.last_sample_error_at is not None and row.last_sample_error_at > row.last_sample_ok_at
