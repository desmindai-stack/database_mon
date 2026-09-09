"""Dönemde yapılanlar (Faz 28 İŞ 5).

Müşteriye DBA ekibinin çalıştığını gösteren şey bu. Rapor bugüne kadar yalnızca "şu anda ne
sorun var" diyordu; emeğin görünmemesi, hizmetin değerinin de görünmemesi demek.

Bu dosyanın koruduğu fikirler:

1. **Tekrar açılanlar ayrı sayılıyor.** "Çözüldü" denip yeniden tespit edilen bir bulgu,
   uygulanan çözümün işe yaramadığını söylüyor; kapatılanlarla aynı kefeye koymak ekibin
   başarısını olduğundan iyi gösterirdi.
2. **Kim ne yaptı yalnızca teknik raporda.** Müşteriye giden belgede kişi adı, hizmetin
   değil bireyin değerlendirilmesine dönüşür.
3. **"Hareket yok" ile "iş yapılmadı" aynı şey değil.**
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.database import SessionLocal, init_db
from app.models import (
    FindingStatusHistory,
    HealthReport,
    Instance,
    ReportFinding,
)
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import work_done_section
from app.services.work_done import collect_work_done, summarize


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    # GLOBAL kapsamlı kayıtlar her kapsama uyuyor: bir testte açılan global bir karar ya da
    # global bir rapor, sonraki testin sayımına sızıp "neden 3 çıktı?" diye saatler yakardı.
    async with SessionLocal() as session:
        await session.execute(delete(FindingStatusHistory))
        await session.execute(delete(ReportFinding))
        await session.execute(delete(HealthReport))
        await session.commit()


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"wd-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _history(instance_id: int, to_status: str, *, when: datetime, from_status: str | None = "open",
             by: str = "ayse", finding_type: str = "performance:slow_query"):
    return FindingStatusHistory(
        fingerprint=uuid.uuid4().hex[:32],
        finding_type=finding_type,
        scope_type="instance",
        scope_id=instance_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=by,
        changed_at=when,
        note="not",
    )


async def _report_with_findings(session, *, generated_at: datetime, findings: list[tuple[str, int]]):
    """`findings`: (change_state, open_since_days) çiftleri."""
    report = HealthReport(
        scope_type="global", scope_id=None, scope_label="Tüm sistem",
        period_start=generated_at - timedelta(days=1), period_end=generated_at,
        generated_at=generated_at, generated_by="manual", status="done",
        overall_status="ok", progress_pct=100, duration_ms=1,
    )
    session.add(report)
    await session.commit()
    for change_state, open_days in findings:
        session.add(
            ReportFinding(
                report_id=report.id, section="performance", severity="warning",
                title="t", detail="d", evidence={"metric": "m"},
                fingerprint=uuid.uuid4().hex[:32], finding_type="performance:slow_query",
                change_state=change_state, open_since_days=open_days,
            )
        )
    await session.commit()
    return report


# --- Sayımlar -----------------------------------------------------------------------------


async def test_decisions_in_the_period_are_counted_by_kind():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(_history(instance.id, "resolved", when=now - timedelta(hours=2)))
        session.add(_history(instance.id, "planned", when=now - timedelta(hours=3)))
        session.add(_history(instance.id, "risk_accepted", when=now - timedelta(hours=4)))
        session.add(_history(instance.id, "ignored", when=now - timedelta(hours=5)))
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["closed"] == 1
    assert work["planned"] == 1
    assert work["risk_accepted"] == 1
    assert work["ignored"] == 1


async def test_decisions_outside_the_period_are_not_counted():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(_history(instance.id, "resolved", when=now - timedelta(days=10)))
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["closed"] == 0


async def test_decisions_for_other_instances_are_not_counted():
    async with SessionLocal() as session:
        mine = await _instance(session)
        other = await _instance(session)
        now = datetime.now(UTC)
        session.add(_history(other.id, "resolved", when=now - timedelta(hours=1)))
        await session.commit()

        work = await collect_work_done(session, [mine], now - timedelta(days=1), now)

    assert work["closed"] == 0


async def test_a_global_decision_counts_for_every_scope():
    """Karar global verilmiş olabilir; yalnızca tam eşleşen kapsamı aramak, verilmiş bir
    kararı raporda hiç göstermemek olurdu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        row = _history(instance.id, "planned", when=now - timedelta(hours=1))
        row.scope_type = "global"
        row.scope_id = None
        session.add(row)
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["planned"] == 1


# --- Tekrar açılanlar ---------------------------------------------------------------------


async def test_reopened_findings_are_counted_separately_from_closures():
    """Kapatılanlarla aynı kefeye koymak, ekibin başarısını olduğundan iyi gösterirdi."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(
            _history(instance.id, "open", from_status="resolved", when=now - timedelta(hours=1))
        )
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["reopened"] == 1
    assert work["closed"] == 0
    assert len(work["reopened_findings"]) == 1


async def test_reopening_from_pending_verification_also_counts():
    """Ekip "çözüldü, doğrulanacak" dedi ve bulgu yeniden tespit edildi — bu da bir
    başarısızlık sinyali."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(
            _history(
                instance.id, "open", from_status="resolved_pending_verification",
                when=now - timedelta(hours=1),
            )
        )
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["reopened"] == 1


async def test_a_plain_reopen_is_not_a_reopened_finding():
    """Ertelemesi dolan bir bulgunun açığa dönmesi "çözüm işe yaramadı" demek DEĞİL."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(
            _history(instance.id, "open", from_status="deferred", when=now - timedelta(hours=1))
        )
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["reopened"] == 0


# --- Açılan bulgular ve çözüm süresi ------------------------------------------------------


async def test_new_findings_in_period_reports_are_counted_as_opened():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        await _report_with_findings(
            session,
            generated_at=now - timedelta(hours=2),
            findings=[("new", 0), ("new", 0), ("ongoing", 5)],
        )

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["opened"] == 2


async def test_average_resolution_time_comes_from_how_long_findings_were_open():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        await _report_with_findings(
            session,
            generated_at=now - timedelta(hours=2),
            findings=[("resolved", 4), ("resolved", 10)],
        )

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["average_resolution_days"] == pytest.approx(7.0)
    assert work["resolution_sample_size"] == 2


async def test_average_resolution_is_none_without_closures():
    """Kanıt yoksa sayı uydurulmuyor."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)
    assert work["average_resolution_days"] is None


# --- Kişi bazında döküm -------------------------------------------------------------------


async def test_who_did_what_is_collected_for_the_technical_report():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(_history(instance.id, "resolved", when=now - timedelta(hours=1), by="ayse"))
        session.add(_history(instance.id, "resolved", when=now - timedelta(hours=2), by="ayse"))
        session.add(_history(instance.id, "planned", when=now - timedelta(hours=3), by="mehmet"))
        await session.commit()

        work = await collect_work_done(session, [instance], now - timedelta(days=1), now)

    assert work["by_person"][0] == {"person": "ayse", "count": 2}
    assert {row["person"] for row in work["by_person"]} == {"ayse", "mehmet"}


# --- Özet cümlesi -------------------------------------------------------------------------


def test_summary_mentions_reopened_findings():
    """Uygulanan çözümün işe yaramadığını gizlemek, raporu satış aracına çevirmek olurdu."""
    text = summarize({"opened": 3, "closed": 5, "reopened": 2, "average_resolution_days": 4.0})
    assert "tekrar açıldı" in text
    assert "5 konu kapatıldı" in text
    assert "4.0 gün" in text


def test_summary_says_nothing_happened_without_inventing_activity():
    assert "değişiklik olmadı" in summarize({})


# --- Rapor bölümü -------------------------------------------------------------------------


def _ctx(session, instances, hours: int = 24) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(hours=hours), period_end=end,
        previous=None, previous_findings={},
    )


async def test_section_reports_the_period_activity():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(_history(instance.id, "resolved", when=now - timedelta(hours=1)))
        await session.commit()

        result = await work_done_section(_ctx(session, [instance]), [])

    assert result.status == "ok"
    assert result.data["closed"] == 1
    # Bölüm bulgu ÜRETMİYOR: yapılan iş bir sorun değil, kritik sayacına girmemeli.
    assert result.findings == []


async def test_section_does_not_claim_no_work_was_done():
    """"Hareket yok" ile "iş yapılmadı" aynı şey değil: dbace yalnızca kendi üzerinden
    verilen kararları görebiliyor."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await work_done_section(_ctx(session, [instance], hours=1), [])

    assert result.status == "unknown"
    assert "GELMEZ" in (result.unknown_reason or "")


async def test_executive_work_done_carries_counts_without_names():
    """Yönetici raporunda kim ne yaptı GÖRÜNMEZ."""
    from app.services.executive_report import build_executive_report

    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        session.add(_history(instance.id, "resolved", when=now - timedelta(hours=1), by="ayse"))
        await session.commit()

        report = await hr.generate_report(
            session, hr.ReportScope("instance", instance.id, instance.name),
            now - timedelta(days=1), now,
        )
        executive = await build_executive_report(session, report)

    assert executive.work_done["closed_findings"] >= 1
    assert "ayse" not in executive.work_done["note"]
    assert "reopened_findings" in executive.work_done


async def test_findings_from_another_scope_report_are_not_counted():
    """Kapsam süzülmeseydi bir müşterinin raporu, başka bir müşterinin raporunda açılan
    bulguları da sayardı."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        other = HealthReport(
            scope_type="customer", scope_id=99999, scope_label="Başka müşteri",
            period_start=now - timedelta(days=1), period_end=now, generated_at=now - timedelta(hours=1),
            generated_by="manual", status="done", overall_status="ok", progress_pct=100, duration_ms=1,
        )
        session.add(other)
        await session.commit()
        session.add(
            ReportFinding(
                report_id=other.id, section="performance", severity="warning", title="t",
                detail="d", evidence={"metric": "m"}, fingerprint=uuid.uuid4().hex[:32],
                finding_type="performance:slow_query", change_state="new", open_since_days=0,
            )
        )
        await session.commit()

        work = await collect_work_done(
            session, [instance], now - timedelta(days=1), now
        )

    assert work["opened"] == 0
