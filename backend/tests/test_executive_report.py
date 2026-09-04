"""Faz 17 İŞ 3 — yönetici (müşteri) raporu.

En kritik kural test ediliyor: teknik detay ASLA sızmamalı. Bulgular kasten sorgu metni,
parametre adı, komut ve host:port içerecek şekilde kurulup yönetici görünümünde hiçbirinin
görünmediği kanıtlanıyor — hem çıktının kendisi taranıyor hem de koruma katmanının çalıştığı
doğrulanıyor.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import (
    Application,
    Customer,
    DatabaseGroup,
    HealthReport,
    Instance,
    ReportFinding,
)
from app.services.credentials import encrypt_secret
from app.services.executive_report import (
    GRADE_AT_RISK,
    GRADE_ATTENTION,
    GRADE_HEALTHY,
    TechnicalLeakError,
    assert_no_technical_leak,
    build_executive_report,
)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _customer_with_app(session) -> tuple[Customer, Application, DatabaseGroup, Instance]:
    suffix = uuid.uuid4().hex[:8]
    customer = Customer(name=f"X Bank {suffix}", type="private")
    session.add(customer)
    await session.commit()
    application = Application(customer_id=customer.id, name=f"boa-{suffix}")
    session.add(application)
    await session.commit()
    group = DatabaseGroup(
        application_id=application.id, name=f"grp-{suffix}", engine="postgresql",
        topology="patroni", environment="prod",
    )
    session.add(group)
    await session.commit()
    instance = Instance(
        name=f"db-{suffix}", engine="postgresql", host="10.20.30.40", port=5432,
        database="postgres", username="postgres", password=encrypt_secret("x"),
        enabled=True, group_id=group.id,
    )
    session.add(instance)
    await session.commit()
    return customer, application, group, instance


async def _report(session, customer, *, findings: list[dict], sections: dict | None = None) -> HealthReport:
    now = datetime.now(UTC)
    report = HealthReport(
        scope_type="customer", scope_id=customer.id, scope_label=customer.name,
        period_start=now - timedelta(days=1), period_end=now, generated_by="schedule",
        overall_status="critical", status="done", progress_pct=100,
        sections=sections or {"order": [], "items": {}},
    )
    session.add(report)
    await session.commit()
    for f in findings:
        session.add(
            ReportFinding(
                report_id=report.id,
                section=f.get("section", "performance"),
                severity=f.get("severity", "critical"),
                title=f["title"],
                detail=f.get("detail", ""),
                evidence=f.get("evidence", {"metric": "m", "value": 1}),
                commands=f.get("commands"),
                related_object_type=f.get("related_object_type", "instance"),
                related_object_id=f.get("related_object_id"),
                fingerprint=f.get("fingerprint", uuid.uuid4().hex[:16]),
                priority=f.get("priority", 100.0),
                change_state=f.get("change_state", "new"),
                acknowledged=f.get("acknowledged", False),
                # Ek İŞ A: risk listesi artık `status` alanına bakıyor.
                status=f.get("status", "ignored" if f.get("acknowledged") else
                             "resolved" if f.get("change_state") == "resolved" else "open"),
                finding_type=f.get("finding_type", f"{f.get('section', 'performance')}:generic"),
                decision_reference=f.get("decision_reference"),
                open_since_days=f.get("open_since_days", 0),
            )
        )
    await session.commit()
    return report


# --- Teknik sızıntı koruması -----------------------------------------------------------


async def test_technical_details_never_reach_the_executive_view():
    """Bulgular kasten teknik: sorgu metni, parametre adı, komut, host:port, dosya yolu."""
    async with SessionLocal() as session:
        customer, application, _group, instance = await _customer_with_app(session)
        report = await _report(
            session,
            customer,
            findings=[
                {
                    "section": "performance",
                    "severity": "critical",
                    "title": "db-01: SELECT * FROM orders WHERE customer_id = $1 yavaşladı",
                    "detail": "shared_buffers yetersiz; 10.20.30.40:5432 üzerinde çalışıyor",
                    "evidence": {"queryid": "123", "query": "SELECT * FROM orders", "value": 5000},
                    "commands": ["VACUUM FULL public.orders;"],
                    "related_object_id": instance.id,
                },
                {
                    "section": "parameters",
                    "severity": "warning",
                    "title": "work_mem parametresi değişti",
                    "detail": "/etc/postgresql/16/main/postgresql.conf içinde 4MB → 64MB",
                    "evidence": {"metric": "pg_settings.work_mem", "value": "64MB"},
                    "related_object_id": instance.id,
                },
            ],
        )
        executive = await build_executive_report(session, report)

    blob = json.dumps(vars(executive), default=str, ensure_ascii=False)
    for forbidden in (
        "SELECT",
        "VACUUM",
        "work_mem",
        "shared_buffers",
        "pg_settings",
        "10.20.30.40",
        "postgresql.conf",
        "queryid",
    ):
        assert forbidden not in blob, f"yönetici raporuna sızdı: {forbidden}"

    # Yine de anlamlı: uygulama adı ve risk alanları görünüyor.
    assert any(r["application"] == application.name for r in executive.risks)
    assert {r["area"] for r in executive.risks} == {"Performans", "Yapılandırma"}


def test_leak_guard_catches_each_forbidden_pattern():
    for text in (
        "SELECT * FROM orders",
        "pg_stat_statements eksik",
        "sunucu 192.168.1.10 üzerinde",
        "db-host:5432 adresinde",
        "/var/log/postgresql/pg.log dosyasında",
        "work_mem değeri düşük",
    ):
        with pytest.raises(TechnicalLeakError):
            assert_no_technical_leak(text, "test")


def test_leak_guard_allows_plain_business_language():
    assert_no_technical_leak(
        "boa uygulamasının veritabanı yaklaşık 45 gün içinde kapasite sınırına ulaşacak.", "test"
    )


async def test_badly_named_application_is_downgraded_not_fatal():
    """Uygulama adı bir IP gibi konmuşsa rapor patlamamalı, genel etikete düşmeli."""
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"Cust {suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name="10.0.0.1")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"g-{suffix}", engine="postgresql",
            topology="standalone", environment="prod",
        )
        session.add(group)
        await session.commit()
        instance = Instance(
            name=f"i-{suffix}", engine="postgresql", host="h", port=5432, database="d",
            username="u", password=encrypt_secret("x"), enabled=True, group_id=group.id,
        )
        session.add(instance)
        await session.commit()

        report = await _report(
            session, customer,
            findings=[{"section": "capacity", "severity": "critical", "title": "t",
                       "related_object_id": instance.id}],
        )
        executive = await build_executive_report(session, report)

    assert executive.risks[0]["application"] == "İzlenen sistem"


# --- İçerik ----------------------------------------------------------------------------


async def test_grade_reflects_findings_and_uptime():
    async with SessionLocal() as session:
        customer, _app, _group, instance = await _customer_with_app(session)

        healthy = await _report(session, customer, findings=[])
        assert (await build_executive_report(session, healthy)).grade == GRADE_HEALTHY

        warned = await _report(
            session, customer,
            findings=[{"section": "alerts", "severity": "warning", "title": "t",
                       "related_object_id": instance.id}],
        )
        assert (await build_executive_report(session, warned)).grade == GRADE_ATTENTION

        risky = await _report(
            session, customer,
            findings=[{"section": "capacity", "severity": "critical", "title": "t",
                       "related_object_id": instance.id}],
        )
        assert (await build_executive_report(session, risky)).grade == GRADE_AT_RISK


async def test_low_uptime_alone_makes_the_grade_risky():
    async with SessionLocal() as session:
        customer, _app, _group, instance = await _customer_with_app(session)
        sections = {
            "order": ["availability"],
            "items": {
                "availability": {
                    "title": "Erişilebilirlik", "status": "critical", "summary": "",
                    "data": {
                        "instances": [
                            {"instance_id": instance.id, "instance": "db", "uptime_pct": 97.5,
                             "outage_count": 3, "outage_seconds": 2160.0, "longest_outage_seconds": 1200.0}
                        ]
                    },
                }
            },
        }
        report = await _report(session, customer, findings=[], sections=sections)
        executive = await build_executive_report(session, report)

    assert executive.grade == GRADE_AT_RISK
    assert executive.availability["uptime_pct"] == 97.5
    assert executive.availability["outage_count"] == 3
    assert executive.availability["longest_outage_seconds"] == 1200.0


async def test_availability_is_grouped_by_application_not_by_host():
    async with SessionLocal() as session:
        customer, application, _group, instance = await _customer_with_app(session)
        sections = {
            "order": ["availability"],
            "items": {
                "availability": {
                    "title": "Erişilebilirlik", "status": "ok", "summary": "",
                    "data": {
                        "instances": [
                            {"instance_id": instance.id, "instance": "db-secret-hostname",
                             "uptime_pct": 99.9, "outage_count": 1, "outage_seconds": 60.0,
                             "longest_outage_seconds": 60.0}
                        ]
                    },
                }
            },
        }
        report = await _report(session, customer, findings=[], sections=sections)
        executive = await build_executive_report(session, report)

    by_app = executive.availability["by_application"]
    assert [b["application"] for b in by_app] == [application.name]
    assert "db-secret-hostname" not in json.dumps(by_app, ensure_ascii=False)


async def test_capacity_risk_states_the_time_horizon_in_plain_language():
    async with SessionLocal() as session:
        customer, application, _group, instance = await _customer_with_app(session)
        report = await _report(
            session, customer,
            findings=[
                {
                    "section": "capacity", "severity": "critical",
                    "title": "db-01: database_size_bytes kapasite riski",
                    "evidence": {"metric": "database_size_bytes", "horizon_days": 45, "value": 1},
                    "related_object_id": instance.id,
                }
            ],
        )
        executive = await build_executive_report(session, report)

    statement = executive.risks[0]["statement"]
    assert "45 gün" in statement
    assert application.name in statement
    assert "database_size_bytes" not in statement


async def test_inventory_reports_counts_and_dr_coverage():
    async with SessionLocal() as session:
        customer, _app, _group, _instance = await _customer_with_app(session)
        report = await _report(session, customer, findings=[])
        executive = await build_executive_report(session, report)

    assert executive.inventory["database_count"] == 1
    assert executive.inventory["group_count"] == 1
    assert executive.inventory["topologies"] == {"patroni": 1}
    assert executive.inventory["environments"] == {"prod": 1}
    # DR düğümü yok — kapsam 0/1 olarak dürüstçe raporlanıyor.
    assert executive.inventory["dr_covered_groups"] == 0
    assert "0/1" in executive.inventory["dr_coverage_note"]


async def test_acknowledged_and_resolved_findings_are_excluded_from_risks():
    async with SessionLocal() as session:
        customer, _app, _group, instance = await _customer_with_app(session)
        report = await _report(
            session, customer,
            findings=[
                {"section": "performance", "severity": "critical", "title": "kabul edilmiş",
                 "acknowledged": True, "related_object_id": instance.id},
                {"section": "alerts", "severity": "critical", "title": "kapanmış",
                 "change_state": "resolved", "related_object_id": instance.id},
            ],
        )
        executive = await build_executive_report(session, report)

    assert executive.risks == []
    assert executive.grade == GRADE_HEALTHY
    assert executive.work_done["closed_findings"] == 1


async def test_trend_compares_with_previous_report():
    async with SessionLocal() as session:
        customer, _app, _group, instance = await _customer_with_app(session)
        previous = await _report(
            session, customer,
            findings=[
                {"section": "performance", "severity": "critical", "title": "a",
                 "related_object_id": instance.id},
                {"section": "alerts", "severity": "critical", "title": "b",
                 "related_object_id": instance.id},
            ],
        )
        current = await _report(
            session, customer,
            findings=[{"section": "performance", "severity": "warning", "title": "a",
                       "related_object_id": instance.id}],
        )
        current.previous_report_id = previous.id
        await session.commit()

        executive = await build_executive_report(session, current)

    assert executive.trend["available"] is True
    assert executive.trend["direction"] == "iyileşti"
    assert executive.trend["previous"]["critical"] == 2
    assert executive.trend["current"]["warning"] == 1


async def test_recommendations_carry_business_impact_and_skip_low_risk():
    async with SessionLocal() as session:
        customer, _app, _group, instance = await _customer_with_app(session)
        report = await _report(
            session, customer,
            findings=[
                {"section": "capacity", "severity": "critical", "title": "t",
                 "related_object_id": instance.id},
                {"section": "schema", "severity": "info", "title": "t2",
                 "related_object_id": instance.id},
            ],
        )
        executive = await build_executive_report(session, report)

    assert len(executive.recommendations) == 1
    recommendation = executive.recommendations[0]
    assert recommendation["priority"] == "yüksek"
    assert recommendation["if_not_done"]
    assert "plansız bir kesinti" in recommendation["if_not_done"]


async def test_one_risk_item_per_area_and_application():
    """Yönetici listesi kısa olmalı — aynı alandan 10 bulgu tek maddeye iner."""
    async with SessionLocal() as session:
        customer, _app, _group, instance = await _customer_with_app(session)
        report = await _report(
            session, customer,
            findings=[
                {"section": "performance", "severity": "warning", "title": f"sorgu {i}",
                 "related_object_id": instance.id, "fingerprint": f"fp{i}"}
                for i in range(10)
            ],
        )
        executive = await build_executive_report(session, report)

    assert len(executive.risks) == 1


async def test_period_label_follows_the_window_length():
    async with SessionLocal() as session:
        customer, _app, _group, _instance = await _customer_with_app(session)
        report = await _report(session, customer, findings=[])
        report.period_start = report.period_end - timedelta(days=30)
        await session.commit()
        executive = await build_executive_report(session, report)

    assert executive.period_label == "Aylık"
