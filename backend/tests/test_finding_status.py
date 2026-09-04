"""Faz 17 Ek İŞ A — bulgu durum makinesi.

Kanıtlananlar: yedi durum, zorunlu not, kapsam seçimi (instance → grup → uygulama → müşteri →
küresel) ve en dar kapsamın kazanması, geçmiş kaydı, ve üç otomatik geçiş — erteleme süresi
dolunca açılma, çözüm doğrulanamayınca açılma (yanlış kapatmaları yakalar), bulgu kaybolunca
çözülme.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import (
    Application,
    Customer,
    DatabaseGroup,
    FindingAcknowledgement,
    FindingStatusHistory,
    Instance,
    ReportFinding,
)
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.finding_status import (
    STATUS_DEFERRED,
    STATUS_IGNORED,
    STATUS_OPEN,
    STATUS_PLANNED,
    STATUS_RESOLVED,
    STATUS_RESOLVED_PENDING,
    STATUS_RISK_ACCEPTED,
    DecisionIndex,
    ScopeMembership,
    apply_decision,
    build_scope_membership,
    make_finding_type,
    resolve_status,
)
from tests.auth_helper import authed_client


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _decision(**over) -> FindingAcknowledgement:
    base = dict(
        fingerprint="fp",
        finding_type="schema:bloat",
        scope_type="instance",
        scope_id=1,
        status=STATUS_IGNORED,
        acknowledged_by="dba",
        acknowledged_at=datetime.now(UTC),
        note="not",
    )
    base.update(over)
    return FindingAcknowledgement(**base)


NOW = datetime.now(UTC)


# --- Tip anahtarı ve kapsam eşleştirme ---------------------------------------------------


def test_finding_type_excludes_the_target_object_id():
    """Tip anahtarı hedef nesneyi içermemeli — yoksa "bu tipi her yerde yoksay" imkânsız olurdu."""
    a = make_finding_type("availability", ("collection_gap", "42"))
    b = make_finding_type("availability", ("collection_gap", "99"))

    assert a == b == "availability:collection_gap"


def test_scope_membership_matches_each_level():
    membership = ScopeMembership(
        group_of={7: 3}, application_of={7: 2}, customer_of={7: 1}
    )

    assert membership.matches(7, "instance", 7)
    assert membership.matches(7, "group", 3)
    assert membership.matches(7, "application", 2)
    assert membership.matches(7, "customer", 1)
    assert membership.matches(7, "global", None)
    assert not membership.matches(7, "group", 999)
    assert not membership.matches(None, "instance", 7)


def test_narrowest_scope_wins_when_several_decisions_match():
    """Bir sunucu için verilmiş özel karar, aynı tip için verilmiş küresel kararı ezmeli —
    istisna yönetimi böyle çalışır."""
    membership = ScopeMembership(group_of={7: 3}, application_of={7: 2}, customer_of={7: 1})
    global_decision = _decision(
        fingerprint="other", scope_type="global", scope_id=None, status=STATUS_IGNORED
    )
    instance_decision = _decision(fingerprint="fp", scope_type="instance", scope_id=7, status=STATUS_PLANNED)
    index = DecisionIndex([global_decision, instance_decision], membership)

    found = index.find("fp", "schema:bloat", 7)

    assert found is instance_decision


def test_group_scope_decision_covers_a_different_fingerprint_of_the_same_type():
    membership = ScopeMembership(group_of={7: 3}, application_of={7: 2}, customer_of={7: 1})
    decision = _decision(fingerprint="seed-fp", scope_type="group", scope_id=3)
    index = DecisionIndex([decision], membership)

    # Aynı tip, başka bir bulgu (farklı fingerprint) — grup kapsamı yakalamalı.
    assert index.find("baska-fp", "schema:bloat", 7) is decision
    # Başka bir tip yakalanmamalı.
    assert index.find("baska-fp", "schema:baska", 7) is None


# --- Otomatik geçişler --------------------------------------------------------------------


def test_deferred_reopens_when_the_deadline_passes():
    expired = _decision(status=STATUS_DEFERRED, expires_at=NOW - timedelta(hours=1))

    effective = resolve_status(expired, still_detected=True, now=NOW)

    assert effective.status == STATUS_OPEN
    assert effective.auto_transition[:2] == (STATUS_DEFERRED, STATUS_OPEN)
    assert "Süre doldu" in effective.auto_transition[2]


def test_deferred_stays_deferred_before_the_deadline():
    active = _decision(status=STATUS_DEFERRED, expires_at=NOW + timedelta(days=3))

    effective = resolve_status(active, still_detected=True, now=NOW)

    assert effective.status == STATUS_DEFERRED
    assert effective.auto_transition is None


def test_resolved_pending_verification_reopens_when_still_detected():
    """Asıl koruma: "düzelttim" denmişti ama bulgu hâlâ duruyor — yanlış kapatma yakalanır."""
    decision = _decision(status=STATUS_RESOLVED_PENDING)

    effective = resolve_status(decision, still_detected=True, now=NOW)

    assert effective.status == STATUS_OPEN
    assert effective.verification_failed is True
    assert "doğrulanamadı" in effective.auto_transition[2]


def test_resolved_pending_verification_becomes_resolved_when_gone():
    decision = _decision(status=STATUS_RESOLVED_PENDING)

    effective = resolve_status(decision, still_detected=False, now=NOW)

    assert effective.status == STATUS_RESOLVED
    assert effective.verification_failed is False


def test_manual_resolved_is_challenged_if_the_finding_persists():
    decision = _decision(status=STATUS_RESOLVED)

    effective = resolve_status(decision, still_detected=True, now=NOW)

    assert effective.status == STATUS_OPEN
    assert effective.verification_failed is True


def test_finding_that_disappears_becomes_resolved_even_without_a_decision():
    effective = resolve_status(None, still_detected=False, now=NOW)

    assert effective.status == STATUS_RESOLVED
    assert effective.auto_transition[:2] == (STATUS_OPEN, STATUS_RESOLVED)


def test_ignored_and_risk_accepted_pass_through_unchanged():
    for status in (STATUS_IGNORED, STATUS_RISK_ACCEPTED, STATUS_PLANNED):
        effective = resolve_status(_decision(status=status), still_detected=True, now=NOW)
        assert effective.status == status
        assert effective.auto_transition is None


# --- Karar kaydı ve geçmiş ----------------------------------------------------------------


async def test_apply_decision_requires_a_note():
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="not zorunlu"):
            await apply_decision(
                session, fingerprint="fp", finding_type="t", scope_type="instance", scope_id=1,
                status=STATUS_IGNORED, note="   ", changed_by="dba",
            )


async def test_apply_decision_requires_scope_id_for_non_global_scopes():
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="scope_id"):
            await apply_decision(
                session, fingerprint="fp", finding_type="t", scope_type="group", scope_id=None,
                status=STATUS_IGNORED, note="n", changed_by="dba",
            )


async def test_apply_decision_writes_history_with_who_and_when():
    fingerprint = f"fp-{uuid.uuid4().hex[:10]}"
    async with SessionLocal() as session:
        await apply_decision(
            session, fingerprint=fingerprint, finding_type="schema:bloat", scope_type="instance",
            scope_id=5, status=STATUS_DEFERRED, note="salıya bak", changed_by="erdem",
            expires_at=NOW + timedelta(days=3),
        )
        await apply_decision(
            session, fingerprint=fingerprint, finding_type="schema:bloat", scope_type="instance",
            scope_id=5, status=STATUS_PLANNED, note="CR açıldı", changed_by="erdem",
            reference="CHG-42",
        )
        rows = list(
            (await session.execute(
                FindingStatusHistory.__table__.select()
                .where(FindingStatusHistory.fingerprint == fingerprint)
                .order_by(FindingStatusHistory.id.asc())
            )).mappings()
        )

    assert [r["to_status"] for r in rows] == [STATUS_DEFERRED, STATUS_PLANNED]
    assert rows[1]["from_status"] == STATUS_DEFERRED
    assert rows[0]["changed_by"] == "erdem"
    assert rows[0]["note"] == "salıya bak"
    assert rows[1]["reference"] == "CHG-42"


async def test_setting_status_back_to_open_deletes_the_decision():
    fingerprint = f"fp-{uuid.uuid4().hex[:10]}"
    async with SessionLocal() as session:
        await apply_decision(
            session, fingerprint=fingerprint, finding_type="t", scope_type="instance", scope_id=5,
            status=STATUS_IGNORED, note="sustur", changed_by="dba",
        )
        result = await apply_decision(
            session, fingerprint=fingerprint, finding_type="t", scope_type="instance", scope_id=5,
            status=STATUS_OPEN, note="tekrar bakalım", changed_by="dba",
        )
        remaining = list(
            (await session.execute(
                FindingAcknowledgement.__table__.select().where(
                    FindingAcknowledgement.fingerprint == fingerprint
                )
            )).mappings()
        )

    assert result is None
    assert remaining == []


async def test_deadline_is_only_stored_for_statuses_that_can_have_one():
    fingerprint = f"fp-{uuid.uuid4().hex[:10]}"
    async with SessionLocal() as session:
        decision = await apply_decision(
            session, fingerprint=fingerprint, finding_type="t", scope_type="instance", scope_id=5,
            status=STATUS_RISK_ACCEPTED, note="kabul", changed_by="dba",
            expires_at=NOW + timedelta(days=10),
        )

    # Risk kabulünde tarih anlamsız — saklanmamalı, yoksa bulgu beklenmedik şekilde geri açılır.
    assert decision.expires_at is None


# --- Rapor üretimiyle uçtan uca ------------------------------------------------------------


async def _instance(session) -> Instance:
    instance = Instance(
        name=f"st-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


class _Sections:
    def __init__(self, *results):
        self._results = results

    def __enter__(self):
        self._saved = hr._SECTION_BUILDERS[:]
        self._saved_summary = hr._SUMMARY_BUILDERS[:]
        hr._SECTION_BUILDERS.clear()
        hr._SUMMARY_BUILDERS.clear()
        for result in self._results:
            async def builder(_ctx, r=result):
                return r

            hr._SECTION_BUILDERS.append(builder)
        return self

    def __exit__(self, *exc):
        hr._SECTION_BUILDERS.clear()
        hr._SECTION_BUILDERS.extend(self._saved)
        hr._SUMMARY_BUILDERS.extend(self._saved_summary)


def _draft(instance_id: int, key: str = "bloat", severity: str = "critical") -> hr.FindingDraft:
    return hr.FindingDraft(
        section="schema", severity=severity, title=f"Bulgu {key}", detail="d",
        evidence={"metric": "m", "value": 1, "measured_at": NOW.isoformat()},
        fingerprint_parts=(key, str(instance_id)), recommendation="bir şey yapın",
        related_object_type="instance", related_object_id=instance_id,
    )


async def test_report_applies_the_decision_and_excludes_it_from_counters():
    async with SessionLocal() as session:
        instance = await _instance(session)
        scope = hr.ReportScope("instance", instance.id, instance.name)
        draft = _draft(instance.id)
        fingerprint = hr.make_fingerprint("schema", "bloat", str(instance.id))

        await apply_decision(
            session, fingerprint=fingerprint, finding_type="schema:bloat", scope_type="instance",
            scope_id=instance.id, status=STATUS_RISK_ACCEPTED, note="bilinçli karar", changed_by="dba",
        )

        with _Sections(hr.SectionResult(key="schema", title="Şema", status="critical", summary="", findings=[draft])):
            report = await hr.generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )

    assert rows[0]["status"] == STATUS_RISK_ACCEPTED
    assert rows[0]["decision_note"] == "bilinçli karar"
    # Kritik bulgu ama açık değil — raporun genel durumunu kritik yapmamalı.
    assert report.overall_status == "ok"


async def test_report_reopens_a_finding_whose_fix_could_not_be_verified():
    async with SessionLocal() as session:
        instance = await _instance(session)
        scope = hr.ReportScope("instance", instance.id, instance.name)
        draft = _draft(instance.id, key="verify")
        fingerprint = hr.make_fingerprint("schema", "verify", str(instance.id))

        await apply_decision(
            session, fingerprint=fingerprint, finding_type="schema:verify", scope_type="instance",
            scope_id=instance.id, status=STATUS_RESOLVED_PENDING, note="düzelttim", changed_by="dba",
        )

        with _Sections(hr.SectionResult(key="schema", title="Şema", status="critical", summary="", findings=[draft])):
            report = await hr.generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )
        history = list(
            (await session.execute(
                FindingStatusHistory.__table__.select().where(FindingStatusHistory.fingerprint == fingerprint)
            )).mappings()
        )

    assert rows[0]["status"] == STATUS_OPEN
    assert rows[0]["verification_failed"] is True
    assert report.overall_status == "critical", "yanlış kapatma raporu tekrar kritik yapmalı"
    # Otomatik geçiş geçmişe yazılmalı ve "kim" belli olmalı.
    auto = [h for h in history if h["changed_by"] == "sistem"]
    assert auto and auto[-1]["to_status"] == STATUS_OPEN


async def test_report_marks_a_vanished_finding_as_resolved():
    async with SessionLocal() as session:
        instance = await _instance(session)
        scope = hr.ReportScope("instance", instance.id, instance.name)
        draft = _draft(instance.id, key="gone")

        with _Sections(hr.SectionResult(key="schema", title="Şema", status="critical", summary="", findings=[draft])):
            await hr.generate_report(session, scope, *hr.default_period(1))
        with _Sections(hr.SectionResult(key="schema", title="Şema", status="ok", summary="", findings=[])):
            second = await hr.generate_report(session, scope, *hr.default_period(1))

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == second.id)
            )).mappings()
        )

    assert rows[0]["status"] == STATUS_RESOLVED
    assert rows[0]["change_state"] == "resolved"


async def test_customer_scoped_decision_covers_every_instance_of_that_customer():
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"scope-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"app-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"g-{suffix}", engine="postgresql",
            topology="standalone", environment="prod",
        )
        session.add(group)
        await session.commit()
        instance = await _instance(session)
        instance.group_id = group.id
        await session.commit()

        membership = await build_scope_membership(session, [instance])
        decision = _decision(
            fingerprint="seed", finding_type="schema:bloat", scope_type="customer", scope_id=customer.id
        )
        index = DecisionIndex([decision], membership)

    assert index.find("baska-fingerprint", "schema:bloat", instance.id) is decision


# --- Uçlar ---------------------------------------------------------------------------------


async def test_status_endpoint_applies_a_bulk_decision():
    async with await authed_client() as c:
        response = await c.post(
            "/api/reports/findings/status",
            json={
                "findings": [
                    {
                        "fingerprint": f"fp-{uuid.uuid4().hex[:8]}",
                        "finding_type": "schema:bloat",
                        "status": "deferred",
                        "scope_type": "instance",
                        "scope_id": 1,
                        "note": "gelecek haftaya",
                        "until": (NOW + timedelta(days=7)).isoformat(),
                    },
                    {
                        "fingerprint": f"fp-{uuid.uuid4().hex[:8]}",
                        "finding_type": "alerts:noisy_rule",
                        "status": "planned",
                        "scope_type": "instance",
                        "scope_id": 1,
                        "note": "CR açıldı",
                        "reference": "CHG-7",
                    },
                ]
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["status"] for row in body} == {"deferred", "planned"}
    assert any(row["reference"] == "CHG-7" for row in body)


async def test_status_endpoint_rejects_an_empty_note():
    async with await authed_client() as c:
        response = await c.post(
            "/api/reports/findings/status",
            json={"findings": [{"fingerprint": "x", "finding_type": "t", "status": "ignored", "note": ""}]},
        )

    assert response.status_code == 422


async def test_status_history_endpoint_returns_newest_first():
    fingerprint = f"fp-{uuid.uuid4().hex[:8]}"
    async with await authed_client() as c:
        for status, note in (("ignored", "sustur"), ("open", "geri aç")):
            await c.post(
                "/api/reports/findings/status",
                json={
                    "findings": [
                        {
                            "fingerprint": fingerprint, "finding_type": "t", "status": status,
                            "scope_type": "instance", "scope_id": 1, "note": note,
                        }
                    ]
                },
            )
        history = (await c.get(f"/api/reports/findings/{fingerprint}/history")).json()

    assert [h["to_status"] for h in history] == ["open", "ignored"]
    assert history[0]["note"] == "geri aç"


async def test_viewer_cannot_change_a_finding_status():
    async with await authed_client(role="viewer") as c:
        response = await c.post(
            "/api/reports/findings/status",
            json={"findings": [{"fingerprint": "x", "finding_type": "t", "status": "ignored",
                                "scope_type": "instance", "scope_id": 1, "note": "n"}]},
        )

    assert response.status_code == 403
