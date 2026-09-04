"""Faz 17 İŞ 2 — teknik rapor bölümleri.

Her bölüm için: doğru bulguyu ürettiği, kanıtın (evidence) gerçek sayıları taşıdığı, veri
yetersizken "sorunsuz" değil "bilinmiyor" dediği ve gürültü üretmediği kanıtlanıyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal, init_db
from app.models import (
    AlertEvent,
    AlertRule,
    Instance,
    MetricSample,
    PredictionInsight,
    SchemaObjectDailySample,
    SlowQuerySample,
)
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import (
    alerts_section,
    capacity_section,
    changes_section,
    cluster_section,
    executive_summary_section,
    known_issues_section,
    performance_section,
    resources_section,
    schema_section,
)

import pytest


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"sec-{uuid.uuid4().hex[:8]}",
        engine="postgresql",
        host="127.0.0.1",
        port=5432,
        database="postgres",
        username="postgres",
        password=encrypt_secret("x"),
        enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _ctx(session, instances, *, hours: int = 24, previous=None, previous_findings=None) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session,
        scope=hr.ReportScope("global", None, "Tüm sistem"),
        instances=instances,
        period_start=end - timedelta(hours=hours),
        period_end=end,
        previous=previous,
        previous_findings=previous_findings or {},
    )


def _finding(result, needle: str):
    return next(f for f in result.findings if needle in f.title)


# --- Cluster ---------------------------------------------------------------------------


async def test_cluster_section_detects_leader_change_from_stored_snapshots():
    async with SessionLocal() as session:
        instance = await _instance(session, cluster_name="pg-cluster", services=["patroni", "postgresql"])
        now = datetime.now(UTC)
        for offset, leader in ((3000, "node-1"), (2000, "node-1"), (1000, "node-2")):
            session.add(
                MetricSample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(seconds=offset),
                    metrics_json={"cluster_services": {"cluster": {"leader": leader, "has_leader": True}, "services": []}},
                )
            )
        await session.commit()

        result = await cluster_section(_ctx(session, [instance]))

    finding = _finding(result, "lider değişimi")
    assert finding.severity == "warning"
    assert finding.evidence["value"] == 1
    assert finding.evidence["events"][0]["from"] == "node-1"
    assert finding.evidence["events"][0]["to"] == "node-2"
    assert finding.commands, "kritik/uyarı bulgusu aksiyon edilebilir olmalı"


async def test_cluster_section_reports_service_down_periods():
    async with SessionLocal() as session:
        instance = await _instance(session, cluster_name="pg", services=["etcd"])
        now = datetime.now(UTC)
        for offset in (900, 600, 300):
            session.add(
                MetricSample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(seconds=offset),
                    metrics_json={
                        "cluster_services": {
                            "cluster": {"leader": "n1", "has_leader": True},
                            "services": [{"service": "etcd", "status": "down"}],
                        }
                    },
                )
            )
        await session.commit()

        result = await cluster_section(_ctx(session, [instance]))

    finding = _finding(result, "etcd servisi")
    assert finding.severity == "critical"
    assert finding.evidence["value"] == 3


async def test_cluster_section_is_ok_for_standalone_instances():
    """Cluster yapılandırılmamışsa bu bölüm uyarı üretmemeli — yokluğu sorun değil."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await cluster_section(_ctx(session, [instance]))

    assert result.status == "ok"
    assert result.findings == []


async def test_cluster_section_says_unknown_when_snapshots_missing():
    """Cluster tanımlı ama sağlık verisi yoksa "sorunsuz" demek yanlış olur."""
    async with SessionLocal() as session:
        instance = await _instance(session, cluster_name="pg", services=["patroni"])
        result = await cluster_section(_ctx(session, [instance]))

    assert result.status == "unknown"
    assert result.unknown_reason


# --- Performans ------------------------------------------------------------------------


async def test_performance_section_ranks_by_period_delta_and_flags_regression():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        # Önceki dönem (24-48 saat önce): 1000 ms
        for offset, total, calls in ((47, 0.0, 0), (25, 1000.0, 10)):
            session.add(
                SlowQuerySample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(hours=offset),
                    queryid="q1",
                    query="SELECT * FROM orders WHERE customer_id = $1",
                    calls=calls,
                    total_time_ms=total,
                    mean_time_ms=100.0,
                )
            )
        # Bu dönem: 1000 → 6000 (5000 ms artış, %400 kötüleşme)
        for offset, total, calls in ((23, 1000.0, 10), (1, 6000.0, 30)):
            session.add(
                SlowQuerySample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(hours=offset),
                    queryid="q1",
                    query="SELECT * FROM orders WHERE customer_id = $1",
                    calls=calls,
                    total_time_ms=total,
                    mean_time_ms=250.0,
                    shared_blks_read=900,
                    shared_blks_hit=100,
                )
            )
        await session.commit()

        result = await performance_section(_ctx(session, [instance]))

    top = result.data["top_queries"][0]
    assert top["total_time_ms"] == 5000.0, "sıralama kümülatif toplama değil dönem farkına göre"
    assert top["calls"] == 20
    assert top["change"] == "worse"
    assert top["resource"] == "io"

    finding = _finding(result, "kötüleşen pahalı sorgu")
    assert finding.evidence["value"] == 5000.0
    assert finding.evidence["resource"] == "io"


async def test_performance_section_ignores_fast_queries():
    """Ortalaması düşük bir sorgu listede olsa da bulgu üretmemeli — gürültü kontrolü."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset, total in ((23, 0.0), (1, 500.0)):
            session.add(
                SlowQuerySample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(hours=offset),
                    queryid="fast",
                    query="SELECT 1",
                    calls=1000 if offset == 1 else 0,
                    total_time_ms=total,
                    mean_time_ms=0.5,
                )
            )
        await session.commit()

        result = await performance_section(_ctx(session, [instance]))

    assert result.data["top_queries"], "sorgu listede görünmeli"
    assert result.findings == [], "ama bulgu üretilmemeli"


async def test_performance_section_unknown_without_samples():
    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await performance_section(_ctx(session, [instance]))

    assert result.status == "unknown"
    assert "pg_stat_statements" in result.unknown_reason


# --- Kaynak kullanımı ------------------------------------------------------------------


async def test_resources_section_reports_connection_peak_with_evidence():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset, active in ((600, 10), (300, 95), (60, 20)):
            session.add(
                MetricSample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(seconds=offset),
                    active_connections=active,
                    max_connections=100,
                    cache_hit_ratio=99.0,
                )
            )
        await session.commit()

        result = await resources_section(_ctx(session, [instance]))

    finding = _finding(result, "bağlantı doluluğu")
    assert finding.evidence["value"] == 95.0
    assert finding.evidence["active_connections"] == 95
    assert finding.evidence["max_connections"] == 100
    assert finding.severity == "critical"


async def test_resources_section_flags_low_cache_hit():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset, ratio in ((600, 70.0), (300, 80.0)):
            session.add(
                MetricSample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(seconds=offset),
                    active_connections=5,
                    max_connections=100,
                    cache_hit_ratio=ratio,
                )
            )
        await session.commit()

        result = await resources_section(_ctx(session, [instance]))

    finding = _finding(result, "cache hit")
    assert finding.evidence["value"] == 75.0
    assert finding.evidence["threshold"] == 90.0


async def test_resources_section_notes_that_os_metrics_are_not_collected():
    """Dürüstlük: ölçmediğimiz şeyi ölçmüş gibi göstermiyoruz."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            MetricSample(
                instance_id=instance.id, collected_at=datetime.now(UTC), active_connections=1,
                max_connections=100, cache_hit_ratio=99.9,
            )
        )
        await session.commit()
        result = await resources_section(_ctx(session, [instance]))

    assert "CPU/RAM/disk" in result.data["note"]


# --- Şema ------------------------------------------------------------------------------


async def test_schema_section_reports_unused_indexes_and_growth():
    async with SessionLocal() as session:
        instance = await _instance(session)
        today = datetime.now(UTC).date()
        for day_offset, size in ((3, 1_000_000), (0, 4_000_000)):
            session.add(
                SchemaObjectDailySample(
                    instance_id=instance.id,
                    day=today - timedelta(days=day_offset),
                    object_kind="table",
                    schema_name="app",
                    object_name="events",
                    size_bytes=size,
                )
            )
        session.add(
            SchemaObjectDailySample(
                instance_id=instance.id,
                day=today,
                object_kind="index",
                schema_name="app",
                object_name="events.idx_unused",
                size_bytes=500_000,
                extra={"idx_scan": 0},
            )
        )
        await session.commit()

        result = await schema_section(_ctx(session, [instance]))

    unused = _finding(result, "kullanılmayan index")
    assert unused.evidence["value"] == 1
    growth = _finding(result, "app.events")
    assert growth.evidence["growth_per_day"] == pytest.approx(1_000_000, rel=0.01)


async def test_schema_section_unknown_without_daily_snapshots():
    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await schema_section(_ctx(session, [instance]))

    assert result.status == "unknown"
    assert "günde bir" in result.unknown_reason


async def test_schema_section_states_what_it_cannot_measure():
    async with SessionLocal() as session:
        instance = await _instance(session)
        today = datetime.now(UTC).date()
        # İki gün gerekiyor: tek fotoğraftan büyüme çıkarılamaz (Faz 17 İŞ 6).
        for offset in (1, 0):
            session.add(
                SchemaObjectDailySample(
                    instance_id=instance.id, day=today - timedelta(days=offset), object_kind="table",
                    schema_name="app", object_name="t", size_bytes=1,
                )
            )
        await session.commit()
        result = await schema_section(_ctx(session, [instance]))

    assert "Autovacuum gecikmesi" in result.data["note"]


async def test_schema_section_needs_two_days_before_claiming_anything():
    """Tek günlük fotoğraftan "büyüme yok" sonucu çıkarmak yanlış olurdu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            SchemaObjectDailySample(
                instance_id=instance.id, day=datetime.now(UTC).date(), object_kind="table",
                schema_name="app", object_name="t", size_bytes=1,
            )
        )
        await session.commit()
        result = await schema_section(_ctx(session, [instance]))

    assert result.status == "unknown"
    assert "1 gün daha gerekli" in result.unknown_reason
    assert result.data["days_observed"] == 1


# --- Alarmlar --------------------------------------------------------------------------


async def _rule(session, instance_id, name="test-rule", severity="warning") -> AlertRule:
    rule = AlertRule(
        instance_id=instance_id, name=name, metric="active_connections", operator=">",
        threshold=90, severity=severity,
    )
    session.add(rule)
    await session.commit()
    return rule


async def test_alerts_section_flags_noisy_rules():
    async with SessionLocal() as session:
        instance = await _instance(session)
        rule = await _rule(session, instance.id, name="Gürültücü")
        now = datetime.now(UTC)
        for i in range(12):
            session.add(
                AlertEvent(
                    rule_id=rule.id, instance_id=instance.id, metric_value=95.0,
                    message="eşik aşıldı", triggered_at=now - timedelta(minutes=i * 10),
                )
            )
        await session.commit()

        result = await alerts_section(_ctx(session, [instance]))

    finding = _finding(result, "Gürültü yapan alarm")
    assert finding.evidence["value"] == 12
    assert finding.evidence["threshold"] == 10
    assert result.data["rules"][0]["noisy"] is True


async def test_alerts_section_flags_long_open_alert():
    async with SessionLocal() as session:
        instance = await _instance(session)
        rule = await _rule(session, instance.id, name="Eski alarm", severity="critical")
        session.add(
            AlertEvent(
                rule_id=rule.id, instance_id=instance.id, metric_value=99.0,
                message="hâlâ açık", triggered_at=datetime.now(UTC) - timedelta(days=5),
            )
        )
        await session.commit()

        result = await alerts_section(_ctx(session, [instance]))

    finding = _finding(result, "Uzun süredir açık")
    assert finding.severity == "critical"
    assert finding.evidence["value"] == 99.0


async def test_alerts_section_quiet_when_nothing_fired():
    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await alerts_section(_ctx(session, [instance]))

    assert result.status == "ok"
    assert result.findings == []


# --- Kapasite --------------------------------------------------------------------------


async def test_capacity_section_carries_confidence_interval():
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            PredictionInsight(
                instance_id=instance.id, metric_key="database_size_bytes", horizon_minutes=45 * 1440,
                current_value=100.0, predicted_value=200.0, threshold=100.0, confidence=0.8,
                severity="warning", message="Disk 45 gün içinde dolabilir",
                lower_bound=180.0, upper_bound=220.0, recommendation="Arşivleme yapın",
            )
        )
        await session.commit()

        result = await capacity_section(_ctx(session, [instance]))

    finding = _finding(result, "kapasite riski")
    assert finding.evidence["lower_bound"] == 180.0
    assert finding.evidence["upper_bound"] == 220.0
    assert "%90 aralık" in finding.detail
    assert finding.recommendation == "Arşivleme yapın"


# --- Özet bölümleri --------------------------------------------------------------------


async def test_executive_summary_picks_the_highest_priority_items_and_adds_no_findings():
    async with SessionLocal() as session:
        results = [
            hr.SectionResult(
                key="x", title="X", status="critical", summary="",
                findings=[
                    hr.FindingDraft("x", "info", "Önemsiz", "d", {"m": 1}, ("a",)),
                    hr.FindingDraft("x", "critical", "Çok kritik", "d", {"m": 1}, ("b",)),
                    hr.FindingDraft("x", "warning", "Orta", "d", {"m": 1}, ("c",)),
                ],
            )
        ]
        summary = await executive_summary_section(_ctx(session, []), results)

    assert summary.status == "critical"
    assert summary.data["highlights"][0]["title"] == "Çok kritik"
    # "Önemsiz" (info) öne çıkanlara girmemeli.
    assert all(h["severity"] in ("critical", "warning") for h in summary.data["highlights"])
    # Özet kendi bulgusunu üretmez — aynı sorun iki kez sayılmasın.
    assert summary.findings == []


async def test_executive_summary_surfaces_unevaluated_sections():
    async with SessionLocal() as session:
        results = [
            hr.SectionResult(key="a", title="Şema sağlığı", status="unknown", summary="", unknown_reason="veri yok")
        ]
        summary = await executive_summary_section(_ctx(session, []), results)

    assert summary.status == "info"
    assert "Şema sağlığı" in summary.data["unknown_sections"]
    assert "değerlendirilemedi" in summary.summary


async def test_changes_section_says_first_report_when_no_previous():
    async with SessionLocal() as session:
        result = await changes_section(_ctx(session, []), [])

    assert "ilk rapor" in result.summary


async def test_changes_section_classifies_new_regressed_and_resolved():
    async with SessionLocal() as session:
        from app.models import ReportFinding

        gone_fp = hr.make_fingerprint("x", "gone")
        same_fp = hr.make_fingerprint("x", "same")
        previous_findings = {
            gone_fp: ReportFinding(
                report_id=1, section="x", severity="critical", title="Kapanan", detail="",
                fingerprint=gone_fp, change_state="new", open_since_days=2,
            ),
            same_fp: ReportFinding(
                report_id=1, section="x", severity="warning", title="Kötüleşen", detail="",
                fingerprint=same_fp, change_state="ongoing", open_since_days=3,
            ),
        }
        previous = hr.HealthReport(
            id=1, scope_type="global", scope_id=None, scope_label="x",
            period_start=datetime.now(UTC), period_end=datetime.now(UTC),
            generated_at=datetime.now(UTC),
        )
        results = [
            hr.SectionResult(
                key="x", title="X", status="warning", summary="",
                findings=[
                    hr.FindingDraft("x", "critical", "Kötüleşen", "d", {"m": 1}, ("same",)),
                    hr.FindingDraft("x", "warning", "Yeni", "d", {"m": 1}, ("brand-new",)),
                ],
            )
        ]
        ctx = _ctx(session, [], previous=previous, previous_findings=previous_findings)
        result = await changes_section(ctx, results)

    assert [i["title"] for i in result.data["new"]] == ["Yeni"]
    assert [i["title"] for i in result.data["regressed"]] == ["Kötüleşen"]
    assert result.data["regressed"][0]["previous_severity"] == "warning"
    assert [i["title"] for i in result.data["resolved"]] == ["Kapanan"]
    assert result.status == "warning"


async def test_known_issues_section_lists_non_open_decisions_with_their_status():
    """Ek İŞ A: bölüm artık yalnızca "kabul edilenleri" değil, açık olmayan TÜM durumları
    (yoksayıldı/ertelendi/risk kabul/planlandı) kararlarıyla birlikte listeliyor."""
    from app.models import FindingAcknowledgement
    from app.services.report_sections import known_issues_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        draft = hr.FindingDraft(
            section="schema", severity="warning", title="Şişen tablo", detail="d",
            evidence={"metric": "m", "value": 1}, fingerprint_parts=("bloat", str(instance.id)),
            related_object_type="instance", related_object_id=instance.id,
        )
        fingerprint = hr.make_fingerprint("schema", "bloat", str(instance.id))
        session.add(
            FindingAcknowledgement(
                fingerprint=fingerprint,
                finding_type="schema:bloat",
                scope_type="instance",
                scope_id=instance.id,
                status="planned",
                acknowledged_by="dba",
                reference="CHG-1234",
                note="Değişiklik talebi açıldı",
            )
        )
        await session.commit()

        results = [hr.SectionResult(key="schema", title="Şema", status="warning", summary="", findings=[draft])]
        result = await known_issues_section(_ctx(session, [instance]), results)

    assert len(result.data["items"]) == 1
    item = result.data["items"][0]
    assert item["status"] == "planned"
    assert item["status_label"] == "Planlandı"
    assert item["reference"] == "CHG-1234"
    assert item["note"] == "Değişiklik talebi açıldı"
    assert result.data["by_status"] == {"planned": 1}


async def test_known_issues_section_drops_a_decision_whose_deadline_passed():
    """Süresi dolmuş erteleme artık "bilinen konu" değil — açık bir bulgudur."""
    from app.models import FindingAcknowledgement
    from app.services.report_sections import known_issues_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        draft = hr.FindingDraft(
            section="schema", severity="warning", title="Ertelenmişti", detail="d",
            evidence={"metric": "m", "value": 1}, fingerprint_parts=("late", str(instance.id)),
            related_object_type="instance", related_object_id=instance.id,
        )
        session.add(
            FindingAcknowledgement(
                fingerprint=hr.make_fingerprint("schema", "late", str(instance.id)),
                finding_type="schema:late",
                scope_type="instance",
                scope_id=instance.id,
                status="deferred",
                acknowledged_by="dba",
                expires_at=datetime.now(UTC) - timedelta(days=1),
                note="salıya bak",
            )
        )
        await session.commit()

        results = [hr.SectionResult(key="schema", title="Şema", status="warning", summary="", findings=[draft])]
        result = await known_issues_section(_ctx(session, [instance]), results)

    assert result.data["items"] == []


# --- Parametreler ve ön koşullar (günlük durum fotoğrafından) ---------------------------


async def _state(session, instance_id, kind, payload, days_ago=0):
    from app.models import DailyStateSnapshot

    session.add(
        DailyStateSnapshot(
            instance_id=instance_id,
            day=datetime.now(UTC).date() - timedelta(days=days_ago),
            kind=kind,
            payload=payload,
        )
    )
    await session.commit()


async def test_parameters_section_detects_a_manual_change_since_yesterday():
    """Bu bölümün asıl değeri: biri elle ALTER SYSTEM çalıştırdıysa ertesi sabah görünsün."""
    from app.services.report_sections import parameters_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        await _state(
            session, instance.id, "parameters",
            {"findings": [], "values": {"work_mem": "4MB", "max_connections": "100"}}, days_ago=1,
        )
        await _state(
            session, instance.id, "parameters",
            {"findings": [], "values": {"work_mem": "64MB", "max_connections": "100"}}, days_ago=0,
        )

        result = await parameters_section(_ctx(session, [instance]))

    finding = _finding(result, "work_mem parametresi değişti")
    assert finding.evidence["previous_value"] == "4MB"
    assert finding.evidence["value"] == "64MB"
    assert result.data["changes"][0]["parameter"] == "work_mem"
    # Değişmeyen parametre bulgu üretmemeli.
    assert all("max_connections" not in f.title for f in result.findings)


async def test_parameters_section_reports_baseline_deviation():
    from app.services.report_sections import parameters_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        await _state(
            session, instance.id, "parameters",
            {
                "findings": [
                    {
                        "name": "fsync", "severity": "critical", "current_value": "off",
                        "detail": "Mevcut değer 'off', beklenen: on", "recommendation": "fsync=on yapın",
                    }
                ],
                "values": {"fsync": "off"},
            },
        )
        result = await parameters_section(_ctx(session, [instance]))

    finding = _finding(result, "fsync baseline dışında")
    assert finding.severity == "critical"
    assert finding.recommendation == "fsync=on yapın"


async def test_parameters_section_unknown_without_snapshots():
    from app.services.report_sections import parameters_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await parameters_section(_ctx(session, [instance]))

    assert result.status == "unknown"
    assert "günde bir" in result.unknown_reason


async def test_parameters_section_reports_unreadable_instances_instead_of_claiming_ok():
    from app.services.report_sections import parameters_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        await _state(session, instance.id, "parameters", {"error": "connection refused"})
        result = await parameters_section(_ctx(session, [instance]))

    assert instance.name in result.data["unreadable_instances"]
    assert result.unknown_reason


async def test_prerequisites_section_lists_missing_and_separates_ignored():
    from app.services.report_sections import prerequisites_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        await _state(
            session, instance.id, "prerequisites",
            {
                "ignored": ["pg_buffercache"],
                "checks": [
                    {"key": "pg_stat_statements", "name": "pg_stat_statements uzantısı", "status": "missing",
                     "severity": "high", "impact": "Yavaş sorgu listesi boş döner", "fix": "CREATE EXTENSION pg_stat_statements;"},
                    {"key": "pg_buffercache", "name": "pg_buffercache uzantısı", "status": "missing",
                     "severity": "medium", "impact": "manuel analiz", "fix": "CREATE EXTENSION pg_buffercache;"},
                    {"key": "track_io_timing", "name": "track_io_timing", "status": "ok", "severity": "medium",
                     "impact": "Açık"},
                ],
            },
        )
        result = await prerequisites_section(_ctx(session, [instance]))

    finding = _finding(result, "pg_stat_statements uzantısı eksik")
    assert finding.commands == ["CREATE EXTENSION pg_stat_statements;"]
    # Yoksayılan kontrol bulgu üretmez ama listede görünür.
    assert all("pg_buffercache" not in f.title for f in result.findings)
    assert [i["key"] for i in result.data["ignored"]] == ["pg_buffercache"]
    # "ok" olan kontrol hiçbir listede olmamalı.
    assert all(i["key"] != "track_io_timing" for i in result.data["missing"])


async def test_prerequisites_section_does_not_treat_unknown_as_missing():
    """"Kontrol edilemedi" bir eksiklik değil — bulgu üretmemeli."""
    from app.services.report_sections import prerequisites_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        await _state(
            session, instance.id, "prerequisites",
            {"ignored": [], "checks": [{"key": "query_store", "name": "Query Store", "status": "unknown",
                                        "severity": "high", "impact": "okunamadı"}]},
        )
        result = await prerequisites_section(_ctx(session, [instance]))

    assert result.findings == []
    assert result.data["missing"] == []
