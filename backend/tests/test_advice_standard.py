"""Faz 17 Ek İŞ B — öneri ve çözüm adımları standardı.

Kural: rapor, dashboard, DPA ve tahminler AYNI öneri yapısını üretsin — başlık, neden,
numaralı adımlar, her adımın komutu, dikkat notları, tahmini süre, geri alma ve doğrulama.
Öneri üretilemiyorsa nedeni yazılsın, boş bırakılmasın.

Testler tek tek üreticileri değil, hepsinin uyduğu ORTAK sözleşmeyi kontrol ediyor.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, ReportFinding
from app.schemas import AdviceOut, PredictionOut
from app.services import health_report as hr
from app.services.advice import (
    Advice,
    AdviceStep,
    advice_from_playbook,
    advice_to_dict,
    simple_advice,
    unavailable,
)
from app.services.credentials import encrypt_secret


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def assert_valid_advice(payload: dict, *, where: str = "") -> None:
    """Standardın ortak sözleşmesi — her üretici buna uymalı."""
    assert payload is not None, f"{where}: öneri yapısı yok"
    assert payload["title"].strip(), f"{where}: başlıksız öneri"
    # Ya aksiyon edilebilir (adım/neden var) ya da NEDEN üretilemediği yazılı — ikisi de yoksa
    # kullanıcı elinde boş bir kutuyla kalır.
    actionable = bool(payload["steps"]) or bool(payload["why"].strip())
    assert actionable or payload["unavailable_reason"], f"{where}: ne adım ne de gerekçe var"
    for step in payload["steps"]:
        assert step["action"].strip(), f"{where}: eylemsiz adım"
        if step["command"] is not None:
            assert step["command"].strip(), f"{where}: boş komut"


# --- Yapı ve yardımcılar ------------------------------------------------------------------


def test_advice_to_dict_has_every_standard_field():
    advice = Advice(
        title="work_mem değerini artırın",
        why="Sıralama diske taşıyor; yoğun saatlerde sorgular yavaşlıyor.",
        steps=[AdviceStep("Mevcut değeri görün.", "SHOW work_mem;")],
        cautions=["Her bağlantı bu kadar bellek isteyebilir."],
        estimated_duration="Dakikalar",
        rollback="ALTER SYSTEM RESET work_mem;",
        verification="SHOW work_mem;",
    )

    payload = advice_to_dict(advice)

    assert set(payload) == {
        "title", "why", "steps", "cautions", "estimated_duration",
        "rollback", "verification", "unavailable_reason",
    }
    assert payload["steps"] == [{"action": "Mevcut değeri görün.", "command": "SHOW work_mem;"}]
    assert_valid_advice(payload, where="manual")


def test_unavailable_advice_carries_the_reason_not_an_empty_body():
    payload = advice_to_dict(unavailable("Index önerisi için pg_qualstats gerekli."))

    assert payload["unavailable_reason"] == "Index önerisi için pg_qualstats gerekli."
    assert payload["steps"] == []
    assert_valid_advice(payload, where="unavailable")


def test_advice_from_playbook_keeps_commands_on_their_steps():
    playbook = [
        {"title": "Ölçün", "detail": "Önce mevcut durumu görün", "command": "SELECT 1;"},
        {"title": "Uygulayın", "detail": "Sonra düzeltin", "command": None},
    ]

    advice = advice_from_playbook("Diski büyütün", "Disk dolarsa yazma durur.", playbook,
                                  verification="SELECT pg_database_size(current_database());")

    assert advice.steps[0].action == "Ölçün: Önce mevcut durumu görün"
    assert advice.steps[0].command == "SELECT 1;"
    assert advice.steps[1].command is None
    assert advice.verification
    assert_valid_advice(advice_to_dict(advice), where="playbook")


def test_simple_advice_turns_each_command_into_its_own_step():
    advice = simple_advice("Uzantıyı kurun", "Eksikse yavaş sorgu listesi boş kalır.",
                           ["CREATE EXTENSION pg_stat_statements;", "SELECT pg_reload_conf();"])

    assert len(advice.steps) == 2
    assert advice.steps[1].command == "SELECT pg_reload_conf();"


# --- Rapor bulguları -----------------------------------------------------------------------


def _draft(**over) -> hr.FindingDraft:
    base = dict(
        section="schema", severity="warning", title="Bulgu", detail="detay",
        evidence={"metric": "m", "value": 1, "measured_at": "2026-09-04T06:00:00Z"},
        fingerprint_parts=("k",), recommendation="Bir şey yapın", commands=["VACUUM ANALYZE t;"],
    )
    base.update(over)
    return hr.FindingDraft(**base)


def test_findings_without_explicit_advice_still_get_the_standard_shape():
    """Bölümler kademeli olarak zengin öneriye geçebilsin diye motor asgari yapıyı kuruyor."""
    drafts = hr._validated_drafts(
        [hr.SectionResult(key="schema", title="Şema", status="warning", summary="", findings=[_draft()])]
    )

    payload = advice_to_dict(drafts[0].advice)
    assert payload["title"] == "Bir şey yapın"
    assert payload["steps"][0]["command"] == "VACUUM ANALYZE t;"
    assert_valid_advice(payload, where="legacy")


def test_finding_without_a_recommendation_gets_an_unavailable_advice_not_an_empty_one():
    drafts = hr._validated_drafts(
        [
            hr.SectionResult(
                key="schema", title="Şema", status="critical", summary="",
                findings=[_draft(severity="critical", recommendation=None, commands=[])],
            )
        ]
    )

    payload = advice_to_dict(drafts[0].advice)
    assert payload["unavailable_reason"]
    assert "üretilemedi" in payload["unavailable_reason"]
    assert_valid_advice(payload, where="no-recommendation")


def test_info_finding_without_action_says_so_instead_of_showing_an_empty_box():
    drafts = hr._validated_drafts(
        [
            hr.SectionResult(
                key="schema", title="Şema", status="info", summary="",
                findings=[_draft(severity="info", recommendation=None, commands=[])],
            )
        ]
    )

    payload = advice_to_dict(drafts[0].advice)
    assert payload["unavailable_reason"] == "Bu bulgu bilgi amaçlı; ayrı bir aksiyon gerektirmiyor."


def test_section_supplied_advice_is_preserved():
    rich = Advice(title="Özel öneri", why="neden", steps=[AdviceStep("adım", "SELECT 1;")])
    drafts = hr._validated_drafts(
        [hr.SectionResult(key="schema", title="Şema", status="warning", summary="", findings=[_draft(advice=rich)])]
    )

    assert drafts[0].advice is rich


async def test_advice_is_persisted_with_the_finding():
    async with SessionLocal() as session:
        instance = Instance(
            name=f"adv-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"), enabled=True,
        )
        session.add(instance)
        await session.commit()

        scope = hr.ReportScope("instance", instance.id, instance.name)
        draft = _draft(
            advice=Advice(
                title="Index ekleyin", why="Tarama yavaş.",
                steps=[AdviceStep("Oluşturun.", "CREATE INDEX CONCURRENTLY i ON t (c);")],
                cautions=["İşlem bloğunda çalışmaz."], verification="EXPLAIN SELECT 1;",
            ),
            related_object_type="instance", related_object_id=instance.id,
        )
        saved = hr._SECTION_BUILDERS[:]
        saved_summary = hr._SUMMARY_BUILDERS[:]
        hr._SECTION_BUILDERS.clear()
        hr._SUMMARY_BUILDERS.clear()

        async def builder(_ctx):
            return hr.SectionResult(key="schema", title="Şema", status="warning", summary="", findings=[draft])

        hr._SECTION_BUILDERS.append(builder)
        try:
            report = await hr.generate_report(session, scope, *hr.default_period(1))
        finally:
            hr._SECTION_BUILDERS.clear()
            hr._SECTION_BUILDERS.extend(saved)
            hr._SUMMARY_BUILDERS.extend(saved_summary)

        rows = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report.id)
            )).mappings()
        )

    payload = rows[0]["advice"]
    assert payload["title"] == "Index ekleyin"
    assert payload["verification"] == "EXPLAIN SELECT 1;"
    assert payload["cautions"] == ["İşlem bloğunda çalışmaz."]
    assert_valid_advice(payload, where="persisted")


# --- Tahminler ------------------------------------------------------------------------------


def _prediction(**over) -> PredictionOut:
    base = dict(
        id=1, instance_id=1, metric_key="database_size_bytes", created_at=datetime.datetime.now(),
        horizon_minutes=60, current_value=1.0, predicted_value=2.0, threshold=3.0, confidence=0.5,
        severity="warning", message="Disk dolabilir", recommendation="Arşivleyin",
        acknowledged_at=None, playbook=[{"title": "Ölç", "detail": "bak", "command": "SELECT 1;"}],
    )
    base.update(over)
    return PredictionOut(**base)


def test_prediction_playbook_is_exposed_as_standard_advice():
    prediction = _prediction()

    payload = prediction.advice.model_dump()

    assert payload["title"] == "Arşivleyin"
    assert payload["steps"][0]["command"] == "SELECT 1;"
    assert payload["verification"], "her tahmin türü için doğrulama sorgusu olmalı"
    assert payload["cautions"], "yıkıcı komut uyarısı taşımalı"
    assert_valid_advice(payload, where="prediction")


def test_prediction_without_a_playbook_explains_why_there_are_no_steps():
    prediction = _prediction(metric_key="cache_hit_ratio", playbook=[], recommendation=None)

    payload = prediction.advice.model_dump()

    assert payload["unavailable_reason"]
    assert "yanıltıcı" in payload["unavailable_reason"]


def test_every_prediction_type_with_a_playbook_has_a_verification_query():
    for metric in ("database_size_bytes", "transaction_id_age", "connection_utilization_pct",
                   "active_connections", "table_growth:app.events", "index_bloat:app.idx"):
        prediction = _prediction(metric_key=metric)
        assert prediction.advice.verification, f"{metric}: doğrulama sorgusu yok"


def test_prediction_advice_is_not_overwritten_when_supplied():
    prediction = _prediction(advice=AdviceOut(title="Elle verilmiş"))

    assert prediction.advice.title == "Elle verilmiş"


# --- Dashboard ve DPA -----------------------------------------------------------------------


async def test_dashboard_recommendations_carry_standard_advice():
    import app.services.dashboard_snapshot as ds

    group = type("G", (), {"id": 1, "name": "grup", "engine": "postgresql"})()
    recommendations = ds._connectivity_recommendations(
        group, {"down_nodes": [{"node_name": "n1", "site": "primary"}]}
    )

    assert recommendations
    assert_valid_advice(recommendations[0]["advice"], where="dashboard/connectivity")
    assert recommendations[0]["advice"]["verification"]


def test_index_advice_is_converted_to_the_standard_shape():
    from app.routers.queries import _index_advice

    recommendation = type(
        "R", (),
        {
            "table_name": "orders", "schema_name": "app", "columns": ["customer_id"],
            "index_ddl": "CREATE INDEX idx_orders_customer ON app.orders (customer_id);",
            "reason": "WHERE koşulunda filtreleniyor.",
        },
    )()

    payload = _index_advice(recommendation)

    assert_valid_advice(payload, where="dpa/index")
    # Kilitsiz oluşturma tercih edilmeli ve riskleri yazılı olmalı.
    create_step = next(s for s in payload["steps"] if s["command"] and "CREATE INDEX" in s["command"])
    assert "CONCURRENTLY" in create_step["command"]
    assert any("INVALID" in c for c in payload["cautions"])
    assert payload["rollback"].startswith("DROP INDEX CONCURRENTLY")
    assert "idx_orders_customer" in payload["rollback"]
    assert payload["verification"]
