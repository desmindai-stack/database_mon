"""Faz 17 İŞ 6 — raporun işe yarar olmasını belirleyen kalite kuralları.

Bu dosya tek tek bölümleri değil, TÜM bölümlerin uymak zorunda olduğu değişmezleri (invariant)
test eder. Amaç ileriye dönük koruma: yeni bir bölüm eklendiğinde bu kuralları çiğnerse test
kırılır, kimsenin kuralları hatırlamasına gerek kalmaz.

Kurallar:
  1. Gürültü kontrolü — tekrar eden bulgu "N gündür açık" olur, yeni gibi sunulmaz.
  2. Kanıt zorunluluğu — kanıtsız bulgu üretilemez.
  3. Dürüstlük — veri yetersizse "X gün gerekli" denir; ölçülmeyen "sorunsuz" gösterilmez.
  4. Aksiyon edilebilirlik — her kritik/uyarı bulgusunun bir önerisi (ya da neden
     verilemediğinin açıklaması) olur.
  5. Öncelik — bulgular etki × aciliyet ile sıralanır.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import (
    AlertEvent,
    AlertRule,
    Instance,
    MetricSample,
    PredictionInsight,
    ReportFinding,
    SchemaObjectDailySample,
    SlowQuerySample,
)
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.health_report import (
    NO_RECOMMENDATION_EXPLANATION,
    FindingDraft,
    ReportScope,
    SectionResult,
    generate_report,
    registered_sections,
    registered_summary_sections,
)
from app.services.report_sections import needs_more_days, resources_section


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _rich_instance(session) -> Instance:
    """Birçok bölümü aynı anda tetikleyecek kadar veri taşıyan bir instance."""
    instance = Instance(
        name=f"quality-{uuid.uuid4().hex[:8]}",
        engine="postgresql",
        host="127.0.0.1",
        port=5432,
        database="postgres",
        username="postgres",
        password=encrypt_secret("x"),
        enabled=True,
        cluster_name="pg-cluster",
        services=["patroni", "postgresql", "etcd"],
    )
    session.add(instance)
    await session.commit()

    now = datetime.now(UTC)
    # Metrikler: bağlantı zirvesi, düşük cache hit, geçici dosya, toplama boşluğu, lider değişimi.
    for offset, active, cache, leader in (
        (7200, 10, 99.0, "node-1"),
        (3600, 98, 70.0, "node-1"),
        (600, 20, 72.0, "node-2"),
    ):
        session.add(
            MetricSample(
                instance_id=instance.id,
                collected_at=now - timedelta(seconds=offset),
                active_connections=active,
                max_connections=100,
                cache_hit_ratio=cache,
                temp_bytes=1_000_000,
                metrics_json={
                    "cluster_services": {
                        "cluster": {"leader": leader, "has_leader": True},
                        "services": [{"service": "etcd", "status": "down"}],
                    }
                },
            )
        )
    # Yavaş sorgu: dönem içinde belirgin kötüleşme.
    for offset, total, calls in ((23 * 3600, 0.0, 0), (600, 9000.0, 30)):
        session.add(
            SlowQuerySample(
                instance_id=instance.id,
                collected_at=now - timedelta(seconds=offset),
                queryid="q-slow",
                query="SELECT * FROM orders WHERE customer_id = $1",
                calls=calls,
                total_time_ms=total,
                mean_time_ms=300.0,
                shared_blks_read=900,
                shared_blks_hit=100,
            )
        )
    # Kapasite tahmini.
    session.add(
        PredictionInsight(
            instance_id=instance.id,
            metric_key="database_size_bytes",
            horizon_minutes=45 * 1440,
            current_value=100.0,
            predicted_value=200.0,
            threshold=100.0,
            confidence=0.8,
            severity="critical",
            message="Disk 45 gün içinde dolabilir",
            lower_bound=180.0,
            upper_bound=220.0,
        )
    )
    # Şema: iki günlük fotoğraf (büyüme + kullanılmayan index).
    today = now.date()
    for offset, size in ((2, 1_000_000), (0, 5_000_000)):
        session.add(
            SchemaObjectDailySample(
                instance_id=instance.id, day=today - timedelta(days=offset), object_kind="table",
                schema_name="app", object_name="events", size_bytes=size,
            )
        )
    session.add(
        SchemaObjectDailySample(
            instance_id=instance.id, day=today, object_kind="index",
            schema_name="app", object_name="events.idx_unused", size_bytes=900_000,
            extra={"idx_scan": 0},
        )
    )
    # Gürültü yapan alarm kuralı.
    rule = AlertRule(
        instance_id=instance.id, name="Gürültücü kural", metric="active_connections",
        operator=">", threshold=90, severity="warning",
    )
    session.add(rule)
    await session.commit()
    for i in range(12):
        session.add(
            AlertEvent(
                rule_id=rule.id, instance_id=instance.id, metric_value=95.0,
                message="eşik aşıldı", triggered_at=now - timedelta(minutes=i * 5),
            )
        )
    await session.commit()
    return instance


async def _run_all_sections(session, instance) -> list[SectionResult]:
    ctx = hr.ReportContext(
        session=session,
        scope=ReportScope("instance", instance.id, instance.name),
        instances=[instance],
        period_start=datetime.now(UTC) - timedelta(days=1),
        period_end=datetime.now(UTC),
        previous=None,
        previous_findings={},
    )
    results = [await builder(ctx) for builder in registered_sections()]
    results += [await builder(ctx, results) for builder in registered_summary_sections()]
    return results


# --- Kural 2: kanıt zorunluluğu ---------------------------------------------------------


async def test_every_finding_from_every_section_carries_evidence():
    async with SessionLocal() as session:
        instance = await _rich_instance(session)
        results = await _run_all_sections(session, instance)

    findings = [f for r in results for f in r.findings]
    assert findings, "test verisi hiçbir bölümü tetiklemedi — testin kendisi anlamsız olurdu"
    for finding in findings:
        assert finding.evidence, f"kanıtsız bulgu: {finding.section}/{finding.title}"
        # Kanıt ne zaman ölçüldüğünü de söylemeli.
        assert "measured_at" in finding.evidence or "triggered_at" in finding.evidence, finding.title


# --- Kural 4: aksiyon edilebilirlik -----------------------------------------------------


async def test_every_critical_or_warning_finding_has_a_recommendation():
    async with SessionLocal() as session:
        instance = await _rich_instance(session)
        results = await _run_all_sections(session, instance)

    serious = [f for r in results for f in r.findings if f.severity in ("critical", "warning")]
    assert serious
    for finding in serious:
        assert (finding.recommendation or "").strip(), f"önerisiz bulgu: {finding.section}/{finding.title}"


def test_missing_recommendation_is_replaced_with_an_explanation_not_dropped():
    """Öneri üretilemeyen bulgu atılmaz — neden verilemediği yazılır."""
    draft = FindingDraft(
        section="x", severity="critical", title="Önerisiz kritik", detail="d",
        evidence={"metric": "m", "value": 1}, fingerprint_parts=("a",),
    )
    result = SectionResult(key="x", title="X", status="critical", summary="", findings=[draft])

    drafts = hr._validated_drafts([result])

    assert len(drafts) == 1, "bulgu atılmamalı"
    assert drafts[0].recommendation == NO_RECOMMENDATION_EXPLANATION
    assert "üretilemedi" in drafts[0].recommendation


def test_info_findings_may_omit_a_recommendation():
    """Kural yalnızca kritik/uyarı için — bilgi amaçlı bulguya zorla öneri uydurmuyoruz."""
    draft = FindingDraft(
        section="x", severity="info", title="Bilgi", detail="d",
        evidence={"metric": "m", "value": 1}, fingerprint_parts=("a",),
    )
    drafts = hr._validated_drafts([SectionResult(key="x", title="X", status="info", summary="", findings=[draft])])

    assert drafts[0].recommendation is None


# --- Kural 1: gürültü kontrolü ----------------------------------------------------------


async def test_repeating_finding_is_marked_as_open_for_n_days_not_new():
    async with SessionLocal() as session:
        scope = ReportScope("group", uuid.uuid4().int % 2_000_000_000, "gürültü")
        draft = FindingDraft(
            section="x", severity="warning", title="Tekrar eden", detail="d",
            evidence={"metric": "m", "value": 1, "measured_at": "2026-09-04"},
            fingerprint_parts=("same",), recommendation="bir şey yapın",
        )
        saved = hr._SECTION_BUILDERS[:]
        hr._SECTION_BUILDERS.clear()
        saved_summary = hr._SUMMARY_BUILDERS[:]
        hr._SUMMARY_BUILDERS.clear()

        async def builder(_ctx):
            return SectionResult(key="x", title="X", status="warning", summary="", findings=[draft])

        hr._SECTION_BUILDERS.append(builder)
        try:
            for _ in range(3):
                report = await generate_report(session, scope, *hr.default_period(1))
        finally:
            hr._SECTION_BUILDERS.clear()
            hr._SECTION_BUILDERS.extend(saved)
            hr._SUMMARY_BUILDERS.extend(saved_summary)

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )

    assert rows[0]["change_state"] == "ongoing"
    assert rows[0]["open_since_days"] == 2, "üçüncü raporda iki gündür açık olmalı"


# --- Kural 3: dürüstlük -----------------------------------------------------------------


def test_needs_more_days_states_the_remaining_days():
    message = needs_more_days(3, 7, "Tablo büyümesi")

    assert "7 günlük" in message
    assert "3 gün var" in message
    assert "4 gün daha gerekli" in message


def test_needs_more_days_is_positive_when_data_is_sufficient():
    assert "yeterli veri var" in needs_more_days(10, 7, "Tablo büyümesi")


async def test_unmeasured_os_metrics_are_reported_as_unknown_not_healthy():
    """Agent yoksa OS metrikleri "bilinmiyor" olmalı, "sorunsuz" değil."""
    async with SessionLocal() as session:
        instance = await _rich_instance(session)
        ctx = hr.ReportContext(
            session=session, scope=ReportScope("instance", instance.id, instance.name),
            instances=[instance], period_start=datetime.now(UTC) - timedelta(days=1),
            period_end=datetime.now(UTC), previous=None, previous_findings={},
        )
        result = await resources_section(ctx)

    assert result.data["os_metrics"]["status"] == "unknown"
    assert "bilinmiyor" in result.data["os_metrics"]["reason"]


async def test_sections_without_data_say_unknown_rather_than_ok():
    """Veri olmayan bölüm "sorun yok" demez. (Cluster tanımlı ama veri yok senaryosu.)"""
    async with SessionLocal() as session:
        instance = Instance(
            name=f"empty-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"), enabled=True,
            cluster_name="c", services=["patroni"],
        )
        session.add(instance)
        await session.commit()
        results = await _run_all_sections(session, instance)

    by_key = {r.key: r for r in results}
    for key in ("cluster", "performance", "schema", "parameters", "prerequisites"):
        assert by_key[key].status == "unknown", f"{key} veri yokken 'unknown' olmalı"
        assert by_key[key].unknown_reason, f"{key} neden değerlendirilemediğini söylemeli"


# --- Kural 5: öncelik --------------------------------------------------------------------


async def test_findings_are_stored_ordered_by_impact_times_urgency():
    async with SessionLocal() as session:
        instance = await _rich_instance(session)
        report = await generate_report(
            session, ReportScope("instance", instance.id, instance.name), *hr.default_period(1)
        )
        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )

    ranked = sorted(rows, key=lambda r: -r["priority"])
    severities = [r["severity"] for r in ranked if r["change_state"] != "resolved"]
    # En yüksek öncelikli bulgu kritik olmalı; öncelik alfabetik ya da rastgele olamaz.
    assert severities[0] == "critical"
    # Aynı listede daha düşük ciddiyette bir bulgu daha yüksek önceliğe sahip olmamalı.
    critical_min = min((r["priority"] for r in rows if r["severity"] == "critical"), default=0)
    info_max = max((r["priority"] for r in rows if r["severity"] == "info"), default=0)
    assert critical_min > info_max


async def test_executive_summary_never_adds_its_own_findings():
    """Özet bölümü bulgu üretse aynı sorun iki kez sayılır, kritik sayısı şişerdi."""
    async with SessionLocal() as session:
        instance = await _rich_instance(session)
        results = await _run_all_sections(session, instance)

    summary = next(r for r in results if r.key == "executive_summary")
    changes = next(r for r in results if r.key == "changes")
    known = next(r for r in results if r.key == "known_issues")

    assert summary.findings == []
    assert changes.findings == []
    assert known.findings == []
