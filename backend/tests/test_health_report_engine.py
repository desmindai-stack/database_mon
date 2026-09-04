"""Faz 17 İŞ 1 — sağlık raporu motoru.

Kanıtlananlar: fingerprint kararlılığı (değer değişse de aynı), gün-gün karşılaştırma
(new/ongoing/regressed/resolved), kabul edilen bulguların kritik sayısını şişirmemesi, süreli
kabulün dolması, kanıtsız bulgunun reddedilmesi, öncelik sıralaması ve raporun canlı bağlantı
açmadan üretilmesi.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import (
    Customer,
    FindingAcknowledgement,
    HealthReport,
    Instance,
    MetricSample,
    ReportFinding,
)
from app.services import health_report as hr
from app.services.health_report import (
    FindingDraft,
    ReportScope,
    SectionResult,
    generate_report,
    make_fingerprint,
)
from app.services.credentials import encrypt_secret


@pytest.fixture(autouse=True)
async def _schema():
    """Bu modül authed_client kullanmıyor; tabloları kendisi hazırlamalı."""
    await init_db()


def _draft(severity: str = "warning", key: str = "x", **over) -> FindingDraft:
    base = dict(
        section="test",
        severity=severity,
        title=f"Bulgu {key}",
        detail="detay",
        evidence={"metric": "m", "value": 1, "measured_at": "2026-09-04T06:00:00Z"},
        fingerprint_parts=(key,),
    )
    base.update(over)
    return FindingDraft(**base)


def _section(findings: list[FindingDraft], status: str = "warning") -> SectionResult:
    return SectionResult(key="test", title="Test", status=status, summary="özet", findings=findings)


class _Sections:
    """Motoru test ederken gerçek bölüm kayıt listesini geçici olarak değiştirir."""

    def __init__(self, *results: SectionResult):
        self._results = results
        self._saved: list = []

    def __enter__(self):
        self._saved = hr._SECTION_BUILDERS[:]
        hr._SECTION_BUILDERS.clear()
        for result in self._results:
            async def builder(_ctx, r=result):
                return r

            hr._SECTION_BUILDERS.append(builder)
        return self

    def __exit__(self, *exc):
        hr._SECTION_BUILDERS.clear()
        hr._SECTION_BUILDERS.extend(self._saved)


async def _make_instance(session, name_prefix: str = "rep") -> Instance:
    instance = Instance(
        name=f"{name_prefix}-{uuid.uuid4().hex[:8]}",
        engine="postgresql",
        host="127.0.0.1",
        port=5432,
        database="postgres",
        username="postgres",
        password=encrypt_secret("x"),
        enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


def _scope() -> ReportScope:
    """Her test kendi kapsamını alır: test veritabanı çalıştırmalar arasında kalıcı, sabit bir
    scope_id kullanmak önceki koşunun raporlarını "önceki rapor" olarak devralırdı (bulgular
    'new' yerine 'ongoing' gelirdi)."""
    return ReportScope("group", uuid.uuid4().int % 2_000_000_000, "test-kapsam")


def test_fingerprint_ignores_the_measured_value():
    """Değer hash'e girseydi bulgu her gün "yeni" görünür, "kaç gündür açık" hiç işlemezdi."""
    a = make_fingerprint("availability", "collection_gap", "42")
    b = make_fingerprint("availability", "collection_gap", "42")
    c = make_fingerprint("availability", "collection_gap", "43")

    assert a == b
    assert a != c
    assert len(a) == 32


async def test_report_records_findings_with_priority_and_new_state():
    async with SessionLocal() as session:
        with _Sections(_section([_draft("critical", "a"), _draft("info", "b")])):
            report = await generate_report(session, _scope(), *hr.default_period(1))

        assert report.status == "done"
        assert report.overall_status == "critical"
        assert report.duration_ms >= 0

        findings = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )
        assert len(findings) == 2
        assert {f["change_state"] for f in findings} == {"new"}
        # Kritik bulgu info'dan yüksek öncelikli olmalı.
        by_title = {f["title"]: f for f in findings}
        assert by_title["Bulgu a"]["priority"] > by_title["Bulgu b"]["priority"]


async def test_second_report_marks_ongoing_and_counts_open_days():
    async with SessionLocal() as session:
        scope = _scope()
        with _Sections(_section([_draft("warning", "same")])):
            first = await generate_report(session, scope, *hr.default_period(1))
            second = await generate_report(session, scope, *hr.default_period(1))

        assert second.previous_report_id == first.id
        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == second.id)
            )).mappings()
        )
        assert len(rows) == 1
        assert rows[0]["change_state"] == "ongoing"
        assert rows[0]["open_since_days"] == 1


async def test_severity_increase_is_reported_as_regressed():
    async with SessionLocal() as session:
        scope = _scope()
        with _Sections(_section([_draft("warning", "esc")])):
            await generate_report(session, scope, *hr.default_period(1))
        with _Sections(_section([_draft("critical", "esc")])):
            second = await generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == second.id)
            )).mappings()
        )
        assert rows[0]["change_state"] == "regressed"


async def test_disappearing_finding_is_recorded_as_resolved():
    """"Yapılan işler" ve "düzelenler" gerçek veriye dayansın diye kapanan bulgu kaydediliyor."""
    async with SessionLocal() as session:
        scope = _scope()
        with _Sections(_section([_draft("critical", "gone")])):
            await generate_report(session, scope, *hr.default_period(1))
        with _Sections(_section([], status="ok")):
            second = await generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == second.id)
            )).mappings()
        )
        assert len(rows) == 1
        assert rows[0]["change_state"] == "resolved"
        assert rows[0]["severity"] == "ok"
        # Genel durum kapanan bulgu yüzünden kritik kalmamalı.
        assert second.overall_status == "ok"


async def test_acknowledged_finding_does_not_drive_overall_status():
    async with SessionLocal() as session:
        scope = _scope()
        fingerprint = make_fingerprint("test", "acked")
        session.add(
            FindingAcknowledgement(
                fingerprint=fingerprint,
                scope_type="group",
                scope_id=scope.scope_id,
                acknowledged_by="tester",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                note="bilinen konu",
            )
        )
        await session.commit()

        with _Sections(_section([_draft("critical", "acked")])):
            report = await generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )
        assert rows[0]["acknowledged"] is True
        # Bulgu duruyor (silinmiyor) ama raporun genel durumunu kritik yapmıyor.
        assert rows[0]["severity"] == "critical"
        assert report.overall_status == "ok"


async def test_expired_acknowledgement_stops_suppressing():
    async with SessionLocal() as session:
        scope = _scope()
        fingerprint = make_fingerprint("test", "expired")
        session.add(
            FindingAcknowledgement(
                fingerprint=fingerprint,
                scope_type="group",
                scope_id=scope.scope_id,
                acknowledged_by="tester",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()

        with _Sections(_section([_draft("critical", "expired")])):
            report = await generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )
        assert rows[0]["acknowledged"] is False
        assert report.overall_status == "critical"


async def test_finding_without_evidence_is_rejected():
    """Kanıt zorunluluğu kodda zorlanıyor (Faz 17 İŞ 6) — yorum satırı değil."""
    async with SessionLocal() as session:
        scope = _scope()
        bad = _draft("warning", "noevidence")
        bad.evidence = {}
        with _Sections(_section([bad])):
            with pytest.raises(ValueError, match="Kanıtsız"):
                await generate_report(session, scope, *hr.default_period(1))

        failed = (
            await session.execute(
                HealthReport.__table__.select()
                .where(HealthReport.scope_id == scope.scope_id)
                .order_by(HealthReport.id.desc())
            )
        ).mappings().first()
        # Hata sessizce kaybolmuyor: raporun kendisine yazılıyor.
        assert failed["status"] == "failed"
        assert "Kanıtsız" in failed["error"]


async def test_prod_environment_outranks_test_at_equal_severity():
    async with SessionLocal() as session:
        scope = _scope()
        drafts = [
            _draft("warning", "testenv", environment="test", title="Test ortamı"),
            _draft("warning", "prodenv", environment="prod", title="Prod ortamı"),
        ]
        with _Sections(_section(drafts)):
            report = await generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )
        by_title = {r["title"]: r["priority"] for r in rows}
        assert by_title["Prod ortamı"] > by_title["Test ortamı"]


async def test_availability_section_derives_outages_from_collection_gaps():
    """Rapor canlı probe yapmadan, saklanan örneklerdeki boşluklardan kesinti çıkarır."""
    from app.services.report_sections import availability_section

    async with SessionLocal() as session:
        instance = await _make_instance(session, "gap")
        now = datetime.now(UTC)
        # 3 örnek: ilk ikisi 15 sn arayla, sonra 20 dakikalık bir boşluk.
        for offset in (3600, 3585, 2385):
            session.add(
                MetricSample(instance_id=instance.id, collected_at=now - timedelta(seconds=offset))
            )
        await session.commit()

        ctx = hr.ReportContext(
            session=session,
            scope=_scope(),
            instances=[instance],
            period_start=now - timedelta(hours=2),
            period_end=now,
            previous=None,
            previous_findings={},
        )
        result = await availability_section(ctx)

        assert result.status in ("warning", "critical")
        assert result.data["total_outages"] == 1
        finding = result.findings[0]
        # Kanıt gerçek sayıları taşıyor.
        assert finding.evidence["outage_count"] == 1
        assert finding.evidence["longest_seconds"] == pytest.approx(1200, abs=1)
        assert finding.recommendation
        # Dürüstlük: "veritabanı kapalıydı" diye kesin iddia yok.
        assert "kanıtlamaz" in finding.detail


async def test_customer_scope_includes_ungrouped_legacy_instances():
    async with SessionLocal() as session:
        name = f"scope-cust-{uuid.uuid4().hex[:6]}"
        customer = Customer(name=name, type="public")
        session.add(customer)
        await session.commit()

        legacy = await _make_instance(session, "legacy")
        legacy.customer_name = name
        await session.commit()

        found = await hr.resolve_scope_instances(session, ReportScope("customer", customer.id, name))

        assert legacy.id in [i.id for i in found]
