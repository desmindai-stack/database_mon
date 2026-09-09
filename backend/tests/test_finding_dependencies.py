"""Bağımlılık bastırma (Faz 28 İŞ 2).

Bir düğüm düştüğünde rapor 40 bulgu üretiyordu: biri gerçek ("düğüme erişilemiyor"),
39'u onun sonucu. Bu dosyanın koruduğu fikirler:

1. **Kök sebep bir tane, sonuçlar bastırılır** — ve sayaçlara girmez.
2. **Bastırılan bulgu SİLİNMEZ.** Silmek, bastırma kuralı yanlışsa gerçek bir sorunu
   görünmez yapardı. Raporda kalıyor, işaretli ve açılabiliyor.
3. **Kısmi sorun kök sebep değildir.** Bir düğüm günün 20 saati ayaktaysa o 20 saatin
   bulguları GERÇEKTİR; bastırmak veri kaybı olurdu.
4. **Grafik tek yerde** — rapor, dashboard ve alarmlar aynı kaynaktan besleniyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database import SessionLocal, init_db
from app.models import (
    AlertEvent,
    AlertRule,
    Application,
    Customer,
    DatabaseGroup,
    Instance,
    MetricSample,
    ReportFinding,
)
from app.services import health_report as hr
from app.services.alert_engine import evaluate_alerts, evaluate_group_alerts
from app.services.credentials import encrypt_secret
from app.services.dashboard import _issues_from_group_health
from app.services.finding_dependencies import (
    DEPENDENCY_RULES,
    RootCause,
    build_suppression_plan,
    suppressed_alert_metrics,
)
from app.services.health_report import generate_report, make_fingerprint

# Rapor bölümleri import ANINDA kaydoluyor (`@register_section`). Bu dosya tek başına
# çalıştırıldığında modül hiç import edilmezse `generate_report` HİÇ bölüm çalıştırmaz ve
# bomboş bir rapor üretir — testler de sessizce "bulgu yok" diye geçerdi.
import app.services.report_sections  # noqa: F401


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _draft(section: str, parts: tuple[str, ...], *, obj_type="instance", obj_id=1, severity="warning"):
    return SimpleNamespace(
        section=section,
        severity=severity,
        title=f"{section}/{parts[0]}",
        fingerprint_parts=parts,
        related_object_type=obj_type,
        related_object_id=obj_id,
    )


def _plan(drafts, group_of_instance=None):
    fingerprints = {id(d): make_fingerprint(d.section, *d.fingerprint_parts) for d in drafts}
    return fingerprints, build_suppression_plan(drafts, fingerprints, group_of_instance or {1: 10})


# --- Grafiğin kendisi ---------------------------------------------------------------------


def test_every_rule_declares_what_it_suppresses():
    """Hiçbir şey bastırmayan bir kural, sessizce hiçbir işe yaramaz."""
    for rule in DEPENDENCY_RULES:
        assert rule.finding_kinds, rule.root
        assert rule.suppressed_sections or rule.suppressed_kinds or rule.suppressed_alert_metrics
        # Kullanıcıya "neden görmüyorum" cevabı verilebilmeli.
        assert rule.reason, rule.root
        assert rule.label, rule.root


def test_a_root_cause_never_suppresses_its_own_alert():
    """Kök sebep alarmı bastırılsaydı hiç haber gitmezdi — 40 e-postadan çok daha kötü."""
    for rule in DEPENDENCY_RULES:
        assert not (set(rule.alert_metrics) & set(rule.suppressed_alert_metrics)), rule.root


def test_availability_and_cluster_sections_are_never_suppressed():
    """`availability` kök sebebin KENDİSİNİ taşıyor; `cluster` ise host-agent üzerinden gelen
    BAĞIMSIZ bir kanal — veritabanına bağlanılamazken en çok ihtiyaç duyulan bilgi odur."""
    for rule in DEPENDENCY_RULES:
        assert "availability" not in rule.suppressed_sections
        assert "cluster" not in rule.suppressed_sections


# --- Bastırma planı -----------------------------------------------------------------------


def test_unreachable_instance_suppresses_its_dependent_sections():
    drafts = [
        _draft("availability", ("unreachable_now", "1"), severity="critical"),
        _draft("performance", ("slow_query", "1", "k")),
        _draft("resources", ("connection_peak", "1")),
        _draft("parameters", ("parameter_deviation", "1", "work_mem")),
        _draft("backup", ("backup", "1", "backup_age:full")),
        _draft("prerequisites", ("prerequisite", "1", "pgss")),
    ]
    fingerprints, plan = _plan(drafts)
    root = fingerprints[id(drafts[0])]

    assert plan.is_root(root)
    for draft in drafts[1:]:
        assert plan.is_suppressed(fingerprints[id(draft)]), draft.section
        assert plan.suppressed_by[fingerprints[id(draft)]] == root
    assert plan.counts[root] == 5


def test_no_samples_is_also_a_root_cause():
    drafts = [
        _draft("availability", ("no_samples", "1"), severity="critical"),
        _draft("schema", ("object_growth", "1", "t")),
    ]
    fingerprints, plan = _plan(drafts)
    assert plan.is_root(fingerprints[id(drafts[0])])
    assert plan.is_suppressed(fingerprints[id(drafts[1])])


def test_a_closed_collection_gap_is_not_a_root_cause():
    """KISMİ sorun bastırma yapmaz: düğüm günün 20 saati ayaktaysa o saatlerin performans
    bulguları gerçektir ve gizlenmeleri veri kaybı olurdu."""
    drafts = [
        _draft("availability", ("collection_gap", "1"), severity="critical"),
        _draft("performance", ("slow_query", "1", "k")),
    ]
    fingerprints, plan = _plan(drafts)
    assert not plan.roots
    assert not plan.is_suppressed(fingerprints[id(drafts[1])])


def test_suppression_does_not_leak_to_other_instances():
    """Bir düğümün düşmesi, komşusunun bulgularını gizlemez."""
    drafts = [
        _draft("availability", ("unreachable_now", "1"), obj_id=1, severity="critical"),
        _draft("performance", ("slow_query", "2", "k"), obj_id=2),
    ]
    fingerprints, plan = _plan(drafts, {1: 10, 2: 20})
    assert not plan.is_suppressed(fingerprints[id(drafts[1])])


def test_patroni_down_suppresses_leader_findings_only():
    drafts = [
        _draft("cluster", ("service_down", "1", "patroni"), severity="critical"),
        _draft("cluster", ("no_leader", "1"), severity="critical"),
        _draft("cluster", ("split_brain", "1")),
    ]
    fingerprints, plan = _plan(drafts)
    assert plan.is_root(fingerprints[id(drafts[0])])
    assert plan.is_suppressed(fingerprints[id(drafts[1])])
    # Split-brain Patroni'nin kapalı olmasının sonucu DEĞİL; bastırılmamalı.
    assert not plan.is_suppressed(fingerprints[id(drafts[2])])


def test_a_different_service_going_down_does_not_suppress_leader_findings():
    """`haproxy` kapalı olmasının lider seçimiyle ilgisi yok; desen servis adına bakmasaydı
    bu ayrım kaybolurdu."""
    drafts = [
        _draft("cluster", ("service_down", "1", "haproxy"), severity="critical"),
        _draft("cluster", ("no_leader", "1"), severity="critical"),
    ]
    fingerprints, plan = _plan(drafts)
    assert not plan.roots
    assert not plan.is_suppressed(fingerprints[id(drafts[1])])


def test_group_scoped_root_suppresses_findings_of_instances_in_that_group():
    """Quorum kaybı grup seviyesinde; ama "lider yok" bulguları düğüm seviyesinde. Grup
    kapsamı düğümlere inmeseydi tam kaçınmak istediğimiz yığılma kalırdı."""
    drafts = [
        _draft("cluster", ("etcd_quorum", "10"), obj_type="group", obj_id=10, severity="critical"),
        _draft("cluster", ("no_leader", "1"), obj_id=1, severity="critical"),
        _draft("cluster", ("no_leader", "2"), obj_id=2, severity="critical"),
        _draft("cluster", ("no_leader", "3"), obj_id=3, severity="critical"),
    ]
    fingerprints, plan = _plan(drafts, {1: 10, 2: 10, 3: 99})
    root = fingerprints[id(drafts[0])]
    assert plan.is_suppressed(fingerprints[id(drafts[1])])
    assert plan.is_suppressed(fingerprints[id(drafts[2])])
    # Başka gruptaki düğüm etkilenmiyor.
    assert not plan.is_suppressed(fingerprints[id(drafts[3])])
    assert plan.counts[root] == 2


def test_two_root_causes_do_not_suppress_each_other():
    """Hangisinin "daha kök" olduğuna karar vermek için elimizde kanıt yok; yanlış tahmin
    gerçek bir arızayı gizlemek olurdu."""
    drafts = [
        _draft("availability", ("unreachable_now", "1"), severity="critical"),
        _draft("cluster", ("service_down", "1", "patroni"), severity="critical"),
    ]
    fingerprints, plan = _plan(drafts)
    assert len(plan.roots) == 2
    assert not plan.suppressed_by


def test_root_cause_without_a_target_object_suppresses_nothing():
    """Kapsamı belirsiz bir kök sebep neyi bastıracağını bilemez; tahmin etmiyor."""
    drafts = [
        _draft("availability", ("unreachable_now", "1"), obj_type=None, obj_id=None),
        _draft("performance", ("slow_query", "1", "k")),
    ]
    fingerprints, plan = _plan(drafts)
    assert not plan.roots
    assert not plan.is_suppressed(fingerprints[id(drafts[1])])


def test_summary_rows_explain_why_checks_were_skipped():
    drafts = [
        _draft("availability", ("unreachable_now", "1"), severity="critical"),
        _draft("performance", ("slow_query", "1", "k")),
        _draft("backup", ("backup", "1", "x")),
    ]
    _, plan = _plan(drafts)
    rows = plan.summary_rows()
    assert len(rows) == 1
    assert rows[0]["suppressed_count"] == 2
    assert rows[0]["root_cause"] == str(RootCause.UNREACHABLE)
    assert rows[0]["reason"]


# --- Alarm tarafı -------------------------------------------------------------------------


def test_alert_suppression_uses_the_same_graph():
    suppressed = suppressed_alert_metrics({"etcd_quorum_lost": 1})
    assert "cluster_has_leader" in suppressed
    assert "etcd_quorum_lost" not in suppressed


def test_alert_suppression_is_inactive_when_no_root_is_firing():
    assert suppressed_alert_metrics({"etcd_quorum_lost": 0, "patroni_down": 0}) == {}


def test_unmeasured_flag_is_not_treated_as_a_root_cause():
    """`None` "ölçülemedi" demek. Ölçülemeyen bir bayrağı "sorun var" saymak, ölçüm
    eksikliğini bastırmaya çevirirdi."""
    assert suppressed_alert_metrics({"patroni_down": None}) == {}


async def _alert_instance(session) -> Instance:
    instance = Instance(
        name=f"dep-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


async def test_dependent_alert_event_is_not_created_while_the_root_is_firing():
    async with SessionLocal() as session:
        instance = await _alert_instance(session)
        session.add(
            AlertRule(
                instance_id=instance.id, name="Lider yok", metric="cluster_has_leader",
                operator="<", threshold=1.0, enabled=True, rule_type="metric",
            )
        )
        session.add(
            AlertRule(
                instance_id=instance.id, name="Patroni down", metric="patroni_down",
                operator=">", threshold=0.0, enabled=True, rule_type="metric",
            )
        )
        await session.commit()

        await evaluate_alerts(
            session, instance.id, {"patroni_down": 1, "cluster_has_leader": 0}
        )
        await session.commit()

        events = (
            await session.execute(
                AlertEvent.__table__.select().where(AlertEvent.instance_id == instance.id)
            )
        ).mappings().all()

    # Kök sebep alarmı GİTMELİ, bağlı olan gitmemeli: 40 e-posta yerine bir tane.
    metrics = {e["message"].split(":")[0] for e in events}
    assert len(events) == 1
    assert "Patroni down" in metrics


async def test_group_alerts_respect_the_same_suppression():
    async with SessionLocal() as session:
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

        session.add(
            AlertRule(
                group_id=group.id, name="Lider yok", metric="cluster_has_leader",
                operator="<", threshold=1.0, enabled=True, rule_type="metric",
            )
        )
        await session.commit()
        await evaluate_group_alerts(
            session, group.id, {"etcd_quorum_lost": 1, "cluster_has_leader": 0}
        )
        await session.commit()
        events = (
            await session.execute(
                AlertEvent.__table__.select().where(AlertEvent.group_id == group.id)
            )
        ).mappings().all()
    assert events == []


# --- Dashboard ----------------------------------------------------------------------------


def _dash_ctx():
    return {
        "customer": SimpleNamespace(name="X"),
        "application": SimpleNamespace(name="app"),
        "group": SimpleNamespace(id=1, name="g", environment="prod"),
    }


def test_dashboard_drops_the_dependent_issue_but_says_so():
    """Sessizce yok etmek, bastırma kuralı yanlış olduğunda kimsenin fark edememesi demekti."""
    now = datetime.now(UTC)
    issues = _issues_from_group_health(
        _dash_ctx(),
        {
            "etcd_quorum": {"total": 3, "up": 1, "has_quorum": False},
            "cluster": {"has_leader": False},
        },
        now,
    )
    metrics = [i["metric"] for i in issues]
    assert "etcd_quorum_lost" in metrics
    assert "cluster_has_leader" not in metrics
    root = next(i for i in issues if i["metric"] == "etcd_quorum_lost")
    assert "bastırıldı" in root["message"]
    assert root["suppressed_count"] == 1


def test_dashboard_keeps_everything_when_no_root_is_firing():
    now = datetime.now(UTC)
    issues = _issues_from_group_health(
        _dash_ctx(), {"cluster": {"has_leader": False}}, now
    )
    assert [i["metric"] for i in issues] == ["cluster_has_leader"]


# --- Uçtan uca: rapor motoru ---------------------------------------------------------------


async def test_report_marks_the_root_cause_and_excludes_suppressed_from_counters():
    """Uçtan uca: erişilemeyen bir veritabanı için üretilen rapor tek kök sebep gösteriyor,
    bağlı bulgular bastırılmış ve sayaçlara girmiyor."""
    async with SessionLocal() as session:
        instance = Instance(
            name=f"dep-e2e-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"), enabled=True,
        )
        session.add(instance)
        await session.commit()

        now = datetime.now(UTC)
        # Ölçümler var ama bir saat önce kesilmiş: dönem sonunda toplama hâlâ durmuş.
        for offset in (7200, 7140, 7080, 3600):
            session.add(
                MetricSample(
                    instance_id=instance.id,
                    collected_at=now - timedelta(seconds=offset),
                    active_connections=98,
                    max_connections=100,
                    cache_hit_ratio=70.0,
                    temp_bytes=5_000_000,
                )
            )
        await session.commit()

        report = await generate_report(
            session, hr.ReportScope("instance", instance.id, instance.name), now - timedelta(hours=3), now
        )
        rows = (
            await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )
        ).mappings().all()

    roots = [r for r in rows if r["is_root_cause"]]
    suppressed = [r for r in rows if r["suppressed"]]
    assert len(roots) == 1
    assert "veri toplama durmuş" in roots[0]["title"]
    assert suppressed, "bağlı bulguların bastırılması bekleniyordu"

    # Bastırılan bulgular SİLİNMEDİ.
    assert all(r["suppressed_by"] == roots[0]["fingerprint"] for r in suppressed)

    # Sayaçlar bastırılanları saymıyor ve kök sebep en üstte.
    assert report.suppression["suppressed_total"] == len(suppressed)
    assert roots[0]["priority"] == max(r["priority"] for r in rows)


async def test_suppressed_findings_do_not_drive_the_overall_status():
    """Bir düğüm düştüğünde raporun tamamı 40 kritik yüzünden kırmızıya boyanmamalı; asıl
    söylenmesi gereken tek şey düğümün erişilemez olduğu."""
    async with SessionLocal() as session:
        instance = Instance(
            name=f"dep-st-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"), enabled=True,
        )
        session.add(instance)
        await session.commit()
        now = datetime.now(UTC)
        for offset in (7200, 7140, 7080, 3600):
            session.add(
                MetricSample(
                    instance_id=instance.id, collected_at=now - timedelta(seconds=offset),
                    active_connections=98, max_connections=100, cache_hit_ratio=70.0,
                )
            )
        await session.commit()
        report = await generate_report(
            session, hr.ReportScope("instance", instance.id, instance.name), now - timedelta(hours=3), now
        )
        rows = (
            await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )
        ).mappings().all()

    summary = ((report.sections or {}).get("items") or {}).get("executive_summary") or {}
    counted_critical = sum(
        1 for r in rows if r["severity"] == "critical" and not r["suppressed"] and r["status"] == "open"
    )
    assert summary["data"]["critical_count"] == counted_critical
    assert summary["data"]["suppression"]["suppressed_total"] == sum(
        1 for r in rows if r["suppressed"]
    )
    # Özetin ilk maddesi kök sebep olmalı.
    assert summary["data"]["highlights"][0]["is_root_cause"] is True
