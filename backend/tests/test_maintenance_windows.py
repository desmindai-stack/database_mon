"""Bakım pencereleri ve planlı/plansız kesinti ayrımı (Faz 28 İŞ 3a).

Bakım penceresi olmadan erişilebilirlik sayıları dürüst değil: planlı bir bakım için alınan
40 dakikalık kesinti, plansız bir arızayla aynı kefeye giriyor ve aylık %99.9 hedefini tek
başına deliyor.

Ters yönü de aynı ölçüde önemli ve bu dosyanın asıl koruduğu şey: **her kesintiyi "planlı"
göstermek de sayıyı yalancı yapar.** Bu yüzden pencere önceden tanımlanmış olmalı, geriye
dönük genişletilmemeli ve süresi tekrar aralığını aşmamalı.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.database import SessionLocal, init_db
from app.domain.maintenance import (
    MAX_OCCURRENCES,
    MaintenanceRecurrence,
    OutageKind,
    classify_outage,
    expand_occurrences,
    overlap_seconds,
)
from app.models import AlertEvent, AlertRule, Instance, MaintenanceWindow, MetricSample
from app.services import health_report as hr
from app.services.alert_engine import evaluate_alerts
from app.services.credentials import encrypt_secret
from app.services.maintenance import (
    annotate_outages,
    is_in_maintenance,
    occurrences_for_instance,
)
from app.services.report_sections import availability_section

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    # Bakım pencereleri GLOBAL kapsamlı olabiliyor ve testler aynı SQLite dosyasını
    # paylaşıyor: bir testte açılan global pencere, sonrakinde "neden bu kesinti planlı
    # göründü?" diye saatler yakabilirdi. Her test temiz bir tabloyla başlıyor.
    async with SessionLocal() as session:
        await session.execute(delete(MaintenanceWindow))
        await session.commit()


def _dense_samples(instance_id: int, now: datetime, gap: tuple[int, int]) -> list[MetricSample]:
    """15 saniyede bir ölçüm; `gap` aralığında (saniye önce) hiç ölçüm yok.

    Seyrek ve düzensiz örnekler kesinti tespitini eşiğin sınırına oturtuyor ve testin ne
    ölçtüğü belirsizleşiyordu. Yoğun örnekleme tek bir kesinti bırakıyor.
    """
    rows = []
    for seconds in range(3600, 0, -15):
        if gap[1] <= seconds <= gap[0]:
            continue
        rows.append(
            MetricSample(instance_id=instance_id, collected_at=now - timedelta(seconds=seconds))
        )
    return rows


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"mw-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


# --- Tekrar genişletmesi ------------------------------------------------------------------


def test_single_window_is_returned_only_when_it_overlaps_the_query():
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    assert expand_occurrences(start, end, "none", start, end)
    assert not expand_occurrences(
        start, end, "none", datetime(2026, 9, 5, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC)
    )


def test_broken_definition_expands_to_nothing():
    """Bitişi başlangıcından önce olan bir kural sonsuz döngü üretirdi."""
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    assert expand_occurrences(start, start - timedelta(hours=1), "daily", start, start) == []
    assert expand_occurrences(start, start, "daily", start, start) == []


def test_an_old_recurring_rule_still_applies_today():
    """İLERİ SARMA: kural iki yıl önce tanımlanmış olabilir. Örnekleri baştan tek tek üretmek
    MAX_OCCURRENCES sınırına bugüne VARMADAN takılırdı — yani eski bir bakım penceresi
    sessizce hiç uygulanmazdı."""
    start = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    today = datetime(2026, 9, 9, 2, 30, tzinfo=UTC)
    occurrences = expand_occurrences(start, end, "daily", today, today)
    assert len(occurrences) == 1
    assert occurrences[0].covers(today)


def test_daily_expansion_stays_within_the_safety_cap():
    start = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    occurrences = expand_occurrences(
        start, start + timedelta(hours=1), "daily", start, start + timedelta(days=3000)
    )
    assert len(occurrences) <= MAX_OCCURRENCES


def test_monthly_recurrence_does_not_drift_after_a_short_month():
    """31 Ocak'ta tanımlı bir bakım şubatta 29'a çekiliyor ama MARTTA yine 31 olmalı.
    Zincirleme eklemek 29 Mart üretirdi ve bakım her ay bir gün öne kayardı."""
    start = datetime(2024, 1, 31, 2, 0, tzinfo=UTC)
    occurrences = expand_occurrences(
        start, start + timedelta(hours=2), "monthly",
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 4, 5, tzinfo=UTC),
    )
    assert [o.start.date().isoformat() for o in occurrences] == [
        "2024-01-31",
        "2024-02-29",
        "2024-03-31",
    ]


def test_recurrence_is_never_expanded_backwards():
    """Bir pencere tanımlanmadan önceki kesintileri geçmişe dönük "planlı" saymak, sayıyı
    istediğin gibi düzeltebilmek demek olurdu."""
    start = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    occurrences = expand_occurrences(
        start, start + timedelta(hours=2), "daily",
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert occurrences == []


# --- Örtüşme ve sınıflandırma -------------------------------------------------------------


def test_partially_overlapping_outage_is_split_not_stamped():
    """Bakım 02:00-04:00 iken 03:30'da başlayıp 06:00'a kadar süren bir kesinti yarı planlı
    yarı plansızdır. Hepsini planlı saymak arızayı gizler."""
    window = expand_occurrences(
        datetime(2026, 9, 14, 2, 0, tzinfo=UTC), datetime(2026, 9, 14, 4, 0, tzinfo=UTC),
        "none", datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC),
    )
    kind, planned, unplanned = classify_outage(
        datetime(2026, 9, 14, 3, 30, tzinfo=UTC), datetime(2026, 9, 14, 6, 0, tzinfo=UTC), window
    )
    assert planned > 0 and unplanned > 0
    # Çoğunluk plansız → plansız etiketi.
    assert kind == str(OutageKind.UNPLANNED)
    assert unplanned > planned


def test_overlap_never_exceeds_the_outage_itself():
    """Tolerans yüzünden örtüşme kesintiden uzun çıkarsa plansız süre negatife düşerdi."""
    window = expand_occurrences(
        datetime(2026, 9, 14, 2, 0, tzinfo=UTC), datetime(2026, 9, 14, 6, 0, tzinfo=UTC),
        "none", datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC),
    )
    outage_start = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)
    outage_end = outage_start + timedelta(minutes=10)
    assert overlap_seconds(outage_start, outage_end, window) == pytest.approx(600, abs=1)


def test_outage_just_before_the_window_is_still_planned():
    """Bakım 02:00'de başlıyorsa servis 01:59'da durmuş olabilir; tolerans olmadan kesintinin
    baş tarafı plansız sayılır ve tek bir bakım iki parçaya bölünürdü."""
    window = expand_occurrences(
        datetime(2026, 9, 14, 2, 0, tzinfo=UTC), datetime(2026, 9, 14, 4, 0, tzinfo=UTC),
        "none", datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC),
    )
    kind, planned, _ = classify_outage(
        datetime(2026, 9, 14, 1, 58, tzinfo=UTC), datetime(2026, 9, 14, 3, 0, tzinfo=UTC), window
    )
    assert kind == str(OutageKind.PLANNED)
    assert planned > 3000


def test_outage_with_no_windows_is_unplanned():
    kind, planned, unplanned = classify_outage(NOW - timedelta(hours=1), NOW, [])
    assert kind == str(OutageKind.UNPLANNED)
    assert planned == 0
    assert unplanned == pytest.approx(3600, abs=1)


def test_annotate_outages_keeps_the_original_fields():
    rows = annotate_outages(
        [{"start": (NOW - timedelta(hours=1)).isoformat(), "end": NOW.isoformat(), "seconds": 3600.0}],
        [],
    )
    assert rows[0]["seconds"] == 3600.0
    assert rows[0]["kind"] == str(OutageKind.UNPLANNED)


# --- Kapsam hiyerarşisi -------------------------------------------------------------------


async def test_a_global_window_covers_every_instance():
    """Müşteri/global seviyesinde tanımlanan bir pencere her düğümü kapsamalı; aksi halde her
    düğüm için ayrı pencere açmak gerekirdi ve biri unutulduğunda o düğümün kesintisi
    'plansız' görünürdü."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            MaintenanceWindow(
                scope_type="global", scope_id=None, title="Genel bakım",
                starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=1),
                recurrence="none", enabled=True, created_by="admin",
            )
        )
        await session.commit()
        occurrences = await occurrences_for_instance(
            session, instance, NOW - timedelta(hours=2), NOW + timedelta(hours=2)
        )
    assert len(occurrences) == 1


async def test_a_window_for_another_instance_does_not_apply():
    async with SessionLocal() as session:
        instance = await _instance(session)
        other = await _instance(session)
        session.add(
            MaintenanceWindow(
                scope_type="instance", scope_id=other.id, title="Komşunun bakımı",
                starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=1),
                recurrence="none", enabled=True, created_by="admin",
            )
        )
        await session.commit()
        occurrences = await occurrences_for_instance(
            session, instance, NOW - timedelta(hours=2), NOW + timedelta(hours=2)
        )
    assert occurrences == []


async def test_a_disabled_window_is_ignored():
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            MaintenanceWindow(
                scope_type="global", scope_id=None, title="Kapalı",
                starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=1),
                recurrence="none", enabled=False, created_by="admin",
            )
        )
        await session.commit()
        occurrences = await occurrences_for_instance(
            session, instance, NOW - timedelta(hours=2), NOW + timedelta(hours=2)
        )
    assert occurrences == []


async def test_recurrence_until_stops_the_rule():
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            MaintenanceWindow(
                scope_type="global", scope_id=None, title="Biten kural",
                starts_at=NOW - timedelta(days=30), ends_at=NOW - timedelta(days=30) + timedelta(hours=2),
                recurrence="daily", recurrence_until=NOW - timedelta(days=10),
                enabled=True, created_by="admin",
            )
        )
        await session.commit()
        occurrences = await occurrences_for_instance(
            session, instance, NOW - timedelta(days=1), NOW
        )
    assert occurrences == []


# --- Alarm bastırma -----------------------------------------------------------------------


async def test_no_alert_is_created_during_a_maintenance_window():
    """Alarmı üretip "bakımdaydı" diye işaretlemek yetmez: e-posta yine gider ve bakım
    gecelerinde nöbetçiyi uyandırmaya devam ederdi."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            AlertRule(
                instance_id=instance.id, name="Bağlantı doluluğu", metric="connection_utilization",
                operator=">", threshold=50.0, enabled=True, rule_type="metric",
            )
        )
        now = datetime.now(UTC)
        session.add(
            MaintenanceWindow(
                scope_type="instance", scope_id=instance.id, title="Bakım",
                starts_at=now - timedelta(minutes=30), ends_at=now + timedelta(minutes=30),
                recurrence="none", enabled=True, created_by="admin",
            )
        )
        await session.commit()

        await evaluate_alerts(session, instance.id, {"connection_utilization": 99.0})
        await session.commit()
        events = (
            await session.execute(
                AlertEvent.__table__.select().where(AlertEvent.instance_id == instance.id)
            )
        ).mappings().all()
    assert events == []


async def test_alerts_resume_outside_the_window():
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            AlertRule(
                instance_id=instance.id, name="Bağlantı doluluğu", metric="connection_utilization",
                operator=">", threshold=50.0, enabled=True, rule_type="metric",
            )
        )
        now = datetime.now(UTC)
        session.add(
            MaintenanceWindow(
                scope_type="instance", scope_id=instance.id, title="Dün biten bakım",
                starts_at=now - timedelta(days=1), ends_at=now - timedelta(days=1) + timedelta(hours=1),
                recurrence="none", enabled=True, created_by="admin",
            )
        )
        await session.commit()

        assert await is_in_maintenance(session, instance) is None
        await evaluate_alerts(session, instance.id, {"connection_utilization": 99.0})
        await session.commit()
        events = (
            await session.execute(
                AlertEvent.__table__.select().where(AlertEvent.instance_id == instance.id)
            )
        ).mappings().all()
    assert len(events) == 1


# --- Rapor bölümü -------------------------------------------------------------------------


def _ctx(session, instances, period_start, period_end) -> hr.ReportContext:
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=period_start, period_end=period_end, previous=None, previous_findings={},
    )


async def test_planned_outage_does_not_lower_the_uptime_percentage():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        # 15 dakikalık tek bir kesinti, tam olarak bakım penceresine denk geliyor.
        for sample in _dense_samples(instance.id, now, gap=(2400, 1500)):
            session.add(sample)
        session.add(
            MaintenanceWindow(
                scope_type="instance", scope_id=instance.id, title="Planlı bakım",
                starts_at=now - timedelta(seconds=2400), ends_at=now - timedelta(seconds=1500),
                recurrence="none", enabled=True, created_by="admin",
            )
        )
        await session.commit()

        result = await availability_section(
            _ctx(session, [instance], now - timedelta(hours=1), now)
        )

    row = result.data["instances"][0]
    assert row["outage_count"] == 1
    assert row["planned_outage_seconds"] > 800
    assert row["unplanned_outage_seconds"] < 60
    assert row["uptime_pct"] > 99.0
    # Planlı kesinti bulguya dönüşmüyor: onaylanmış bir bakımı her raporda bulgu olarak
    # göstermek, bulgu listesini takvim haline getirirdi.
    assert not [f for f in result.findings if f.fingerprint_parts[0] == "collection_gap"]
    assert result.status == "ok"
    assert result.data["planned_outages"] == 1


async def test_unplanned_outage_still_produces_a_finding():
    """Karşı kontrol: bakım penceresi yokken aynı veri bulgu üretmeli. Yoksa test, bastırmayı
    değil sadece 'hiç bulgu yok' halini doğrulamış olurdu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for sample in _dense_samples(instance.id, now, gap=(2400, 1500)):
            session.add(sample)
        await session.commit()

        result = await availability_section(
            _ctx(session, [instance], now - timedelta(hours=1), now)
        )

    row = result.data["instances"][0]
    assert row["planned_outage_seconds"] == 0
    assert row["uptime_pct"] < 99.0
    assert [f for f in result.findings if f.fingerprint_parts[0] == "collection_gap"]


async def test_ongoing_outage_inside_a_window_is_informational_not_critical():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset in (7200, 7140, 3600):
            session.add(
                MetricSample(instance_id=instance.id, collected_at=now - timedelta(seconds=offset))
            )
        session.add(
            MaintenanceWindow(
                scope_type="instance", scope_id=instance.id, title="Süren bakım",
                starts_at=now - timedelta(seconds=3600), ends_at=now + timedelta(hours=1),
                recurrence="none", enabled=True, created_by="admin",
            )
        )
        await session.commit()

        result = await availability_section(
            _ctx(session, [instance], now - timedelta(hours=3), now)
        )

    ongoing = [f for f in result.findings if f.fingerprint_parts[0] == "unreachable_now"]
    assert len(ongoing) == 1
    # Susturulmuyor — bakımın sürdüğünü bilmek de bilgi — ama kritik değil.
    assert ongoing[0].severity == "info"
    assert "bakım penceresi" in ongoing[0].title.lower()
