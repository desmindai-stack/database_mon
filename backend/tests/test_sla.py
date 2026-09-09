"""SLA takibi (Faz 28 İŞ 3b).

Hedef olmadan erişilebilirlik sayısı bir bilgi ama bir KARAR değil: %99.7 iyi mi kötü mü,
ancak taahhüde göre söylenebilir.

Bu dosyanın koruduğu asıl fikirler:

1. **İki türev sayı asıl kararı veriyor.** "En iyi durum" hedefin altındaysa dönem
   matematiksel olarak kaybedilmiştir; bunu ayın 3'ünde bilmek, 30'unda öğrenmekten farklı
   bir yönetim kararı üretir. "Kalan bütçe" ise doğrudan bakım planlamak için kullanılır.
2. **Ölçüm yokluğu %100 değildir.** İzlenmeyen bir sunucu SLA'yı kurtarır hale gelmemeli.
3. **Planlı bakım hedefi düşürmez** ama plansız kesinti düşürür.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.database import SessionLocal, init_db
from app.models import (
    Application,
    Customer,
    DatabaseGroup,
    Instance,
    MaintenanceWindow,
    MetricSample,
    SlaTarget,
)
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import sla_section
from app.services.sla import (
    PERIOD_MONTHLY,
    PERIOD_QUARTERLY,
    evaluate_target,
    format_duration,
    period_bounds,
)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(MaintenanceWindow))
        await session.execute(delete(SlaTarget))
        await session.commit()


async def _group(session):
    customer = Customer(name=f"c-{uuid.uuid4().hex[:8]}")
    session.add(customer)
    await session.commit()
    application = Application(customer_id=customer.id, name=f"a-{uuid.uuid4().hex[:8]}")
    session.add(application)
    await session.commit()
    group = DatabaseGroup(
        application_id=application.id, name=f"g-{uuid.uuid4().hex[:8]}",
        engine="postgresql", topology="patroni",
    )
    session.add(group)
    await session.commit()
    return group


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"sla-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _samples(instance_id: int, start: datetime, end: datetime, gap: tuple[datetime, datetime] | None = None):
    """15 saniyede bir ölçüm; `gap` aralığında hiç ölçüm yok."""
    rows = []
    cursor = start
    while cursor <= end:
        if gap is None or not (gap[0] <= cursor <= gap[1]):
            rows.append(MetricSample(instance_id=instance_id, collected_at=cursor))
        cursor += timedelta(seconds=15)
    return rows


# --- Dönem sınırları ----------------------------------------------------------------------


def test_monthly_period_covers_the_whole_month():
    start, end = period_bounds(PERIOD_MONTHLY, datetime(2026, 9, 14, 12, 0, tzinfo=UTC))
    assert start == datetime(2026, 9, 1, tzinfo=UTC)
    assert end == datetime(2026, 10, 1, tzinfo=UTC)


def test_monthly_period_rolls_over_the_year():
    start, end = period_bounds(PERIOD_MONTHLY, datetime(2026, 12, 20, tzinfo=UTC))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_quarterly_period_snaps_to_the_quarter():
    for month, expected_start, expected_end in (
        (1, 1, 4), (2, 1, 4), (5, 4, 7), (8, 7, 10), (11, 10, 1),
    ):
        start, end = period_bounds(PERIOD_QUARTERLY, datetime(2026, month, 15, tzinfo=UTC))
        assert start.month == expected_start
        assert end.month == expected_end


def test_period_end_is_the_end_of_the_period_not_today():
    """"Kalan bütçe" ve "en iyi durum" hesapları kalan süreyi bilmek zorunda; bitişi bugüne
    çekmek ikisini de anlamsız yapardı."""
    now = datetime(2026, 9, 3, tzinfo=UTC)
    _, end = period_bounds(PERIOD_MONTHLY, now)
    assert end > now


# --- Süre biçimi --------------------------------------------------------------------------


def test_negative_budget_is_shown_as_negative_not_hidden():
    """Bütçe aşıldıysa bunu gizlemek, ihlali gizlemek olurdu."""
    assert format_duration(-600).startswith("-")


# --- Değerlendirme ------------------------------------------------------------------------


async def _target(session, instance, target_pct=99.9, period=PERIOD_MONTHLY) -> SlaTarget:
    target = SlaTarget(
        scope_type="instance", scope_id=instance.id, target_pct=target_pct,
        period=period, enabled=True, created_by="admin",
    )
    session.add(target)
    await session.commit()
    return target


async def test_uninterrupted_period_meets_the_target():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=2))
        for sample in _samples(instance.id, start, now):
            session.add(sample)
        target = await _target(session, instance)
        await session.commit()

        status = await evaluate_target(session, target, now)

    assert status.measured
    assert status.met is True
    assert status.achieved_pct == pytest.approx(100.0, abs=0.01)
    assert status.remaining_budget_seconds > 0
    assert status.already_lost is False


async def test_unplanned_outage_consumes_the_budget():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=3))
        gap = (now - timedelta(minutes=90), now - timedelta(minutes=60))
        for sample in _samples(instance.id, start, now, gap=gap):
            session.add(sample)
        target = await _target(session, instance, target_pct=99.9)
        await session.commit()

        status = await evaluate_target(session, target, now)

    assert status.measured
    assert status.unplanned_seconds > 1500
    assert status.achieved_pct < 100.0


async def test_planned_maintenance_does_not_consume_the_budget():
    """Aynı kesinti, bakım penceresi tanımlıyken hedefi düşürmemeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=3))
        gap_start, gap_end = now - timedelta(minutes=90), now - timedelta(minutes=60)
        for sample in _samples(instance.id, start, now, gap=(gap_start, gap_end)):
            session.add(sample)
        session.add(
            MaintenanceWindow(
                scope_type="instance", scope_id=instance.id, title="Planlı",
                starts_at=gap_start, ends_at=gap_end, recurrence="none",
                enabled=True, created_by="admin",
            )
        )
        target = await _target(session, instance)
        await session.commit()

        status = await evaluate_target(session, target, now)

    assert status.planned_seconds > 1500
    assert status.unplanned_seconds < 60
    assert status.met is True


async def test_no_samples_is_not_treated_as_perfect_uptime():
    """İzlenmeyen bir sunucu SLA'yı kurtarır hale gelmemeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        target = await _target(session, instance)
        status = await evaluate_target(session, target, datetime.now(UTC))

    assert status.measured is False
    assert status.achieved_pct is None
    assert status.met is None
    assert "GELMEZ" in (status.unknown_reason or "")


async def test_an_unmeasurable_instance_does_not_lift_the_average():
    """Kapsamda bir ölçülen bir ölçülemeyen instance varsa, ölçülemeyen ortalamayı
    yukarı çekmemeli."""
    async with SessionLocal() as session:
        measured = await _instance(session)
        await _instance(session, group_id=None)  # hiç ölçümü yok
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=3))
        gap = (now - timedelta(minutes=90), now - timedelta(minutes=60))
        for sample in _samples(measured.id, start, now, gap=gap):
            session.add(sample)
        target = SlaTarget(
            scope_type="instance", scope_id=measured.id, target_pct=99.9,
            period=PERIOD_MONTHLY, enabled=True, created_by="admin",
        )
        session.add(target)
        await session.commit()

        status = await evaluate_target(session, target, now)

    # Yalnızca ölçülebilen instance ortalamaya giriyor.
    assert status.measured
    assert len([r for r in status.instances if r.uptime_pct is not None]) == 1


async def test_best_case_shows_a_month_that_is_already_lost():
    """Kalan süre kusursuz geçse bile hedef tutmuyorsa dönem matematiksel olarak
    kaybedilmiştir — ve bunu erken bilmek asıl değerli olan şey."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, period_end = period_bounds(PERIOD_MONTHLY, now)
        # Dönemin başından beri hiç veri yok gibi davranmak yerine, ölçülen kısa bir pencerede
        # büyük bir kesinti üretiyoruz ve hedefi imkânsız derecede yüksek tutuyoruz.
        start = max(period_start, now - timedelta(hours=6))
        gap = (now - timedelta(hours=5), now - timedelta(hours=1))
        for sample in _samples(instance.id, start, now, gap=gap):
            session.add(sample)
        target = await _target(session, instance, target_pct=99.999)
        await session.commit()

        status = await evaluate_target(session, target, now)

    assert status.measured
    assert status.best_case_pct is not None
    assert status.best_case_pct < status.target_pct
    assert status.already_lost is True
    assert status.remaining_budget_seconds < 0


async def test_best_case_is_never_worse_than_achieved():
    """Kalan süre kesintisiz varsayıldığı için en iyi durum, gerçekleşenden düşük olamaz."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=3))
        gap = (now - timedelta(minutes=100), now - timedelta(minutes=40))
        for sample in _samples(instance.id, start, now, gap=gap):
            session.add(sample)
        target = await _target(session, instance)
        await session.commit()
        status = await evaluate_target(session, target, now)

    assert status.best_case_pct >= status.achieved_pct


async def test_worst_instance_is_reported_next_to_the_average():
    """Ortalama, en kötü düğümü gizler; DBA'nın hangi düğüme bakacağını bilmesi gerekiyor."""
    async with SessionLocal() as session:
        # Kapsam GRUP: `global` kullanmak, aynı SQLite dosyasını paylaşan diğer test
        # dosyalarının bıraktığı instance'ları da içine alır ve test ne ölçtüğünü kaybeder.
        group = await _group(session)
        good = await _instance(session, group_id=group.id)
        bad = await _instance(session, group_id=group.id)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=3))
        for sample in _samples(good.id, start, now):
            session.add(sample)
        for sample in _samples(
            bad.id, start, now, gap=(now - timedelta(minutes=90), now - timedelta(minutes=30))
        ):
            session.add(sample)
        target = SlaTarget(
            scope_type="group", scope_id=group.id, target_pct=99.9,
            period=PERIOD_MONTHLY, enabled=True, created_by="admin",
        )
        session.add(target)
        await session.commit()

        status = await evaluate_target(session, target, now)

    assert {r.instance_name for r in status.instances} == {good.name, bad.name}
    assert status.worst_instance == bad.name


# --- Rapor bölümü -------------------------------------------------------------------------


def _ctx(session, instances, period_start, period_end) -> hr.ReportContext:
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=period_start, period_end=period_end, previous=None, previous_findings={},
    )


async def test_section_says_unknown_when_no_target_is_defined():
    """"SLA tutuyor" DEMİYORUZ: hedef tanımlı değilse tutup tutmadığı bilinemez."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        result = await sla_section(_ctx(session, [instance], now - timedelta(days=1), now))

    assert result.status == "unknown"
    assert "hedef" in (result.unknown_reason or "").lower()


async def test_section_produces_a_critical_finding_for_a_lost_period():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=6))
        for sample in _samples(
            instance.id, start, now, gap=(now - timedelta(hours=5), now - timedelta(hours=1))
        ):
            session.add(sample)
        await _target(session, instance, target_pct=99.999)
        await session.commit()

        result = await sla_section(_ctx(session, [instance], now - timedelta(days=1), now))

    assert result.status == "critical"
    finding = next(f for f in result.findings if f.fingerprint_parts[0] == "sla_lost")
    assert finding.advice is not None
    # Öneri beş parçalı standarda uymalı.
    assert finding.advice.why and finding.advice.steps and finding.advice.cautions
    assert finding.advice.verification
    # Sınırlılık açıkça söyleniyor.
    assert any("uygulama erişilebilirliği değil" in c for c in finding.advice.cautions)


async def test_section_is_ok_when_targets_are_met():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        period_start, _ = period_bounds(PERIOD_MONTHLY, now)
        start = max(period_start, now - timedelta(hours=2))
        for sample in _samples(instance.id, start, now):
            session.add(sample)
        await _target(session, instance, target_pct=99.0)
        await session.commit()

        result = await sla_section(_ctx(session, [instance], now - timedelta(days=1), now))

    assert result.status == "ok"
    assert result.data["targets"][0]["met"] is True


# --- Yönetici raporu ----------------------------------------------------------------------


def test_executive_sla_statement_never_leaks_technical_detail():
    from app.services.executive_report import _sla_from_sections, assert_no_technical_leak

    sections = {
        "items": {
            "sla": {
                "data": {
                    "targets": [
                        {
                            "measured": True,
                            "scope_label": "X Bank",
                            "period_label": "Aylık",
                            "target_pct": 99.9,
                            "achieved_pct": 99.5,
                            "best_case_pct": 99.6,
                            "remaining_budget_seconds": -120.0,
                            "met": False,
                            "already_lost": True,
                            "planned_seconds": 0.0,
                            "unplanned_seconds": 3600.0,
                        },
                        {"measured": False, "scope_label": "Y", "period_label": "Aylık", "target_pct": 99.9},
                    ]
                }
            }
        }
    }
    rows = _sla_from_sections(sections)
    assert len(rows) == 2
    for row in rows:
        assert_no_technical_leak(row["statement"], "test")
    # Ölçülemeyen kapsam "uygun değil" demiyor.
    assert rows[1]["met"] is None
    assert "belirlenemiyor" in rows[1]["statement"]


def test_executive_sla_omits_the_worst_instance_name():
    """Sunucu adı yönetici raporuna giremez (CLAUDE.md kuralı)."""
    from app.services.executive_report import _sla_from_sections

    rows = _sla_from_sections(
        {
            "items": {
                "sla": {
                    "data": {
                        "targets": [
                            {
                                "measured": True,
                                "scope_label": "X Bank",
                                "period_label": "Aylık",
                                "target_pct": 99.9,
                                "achieved_pct": 99.95,
                                "best_case_pct": 99.97,
                                "remaining_budget_seconds": 600.0,
                                "met": True,
                                "already_lost": False,
                                "worst_instance": "pg-prod-01",
                                "planned_seconds": 0.0,
                                "unplanned_seconds": 0.0,
                            }
                        ]
                    }
                }
            }
        }
    )
    assert "worst_instance" not in rows[0]
    assert "pg-prod-01" not in rows[0]["statement"]
