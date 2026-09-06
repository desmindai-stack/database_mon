"""Faz 18 İŞ 3 — öneri geçerliliği.

Kural: bir öneri başka bir sayfaya yönlendiriyorsa oradaki içeriğin var olduğu doğrulanmış
olmalı. "DPA'da EXPLAIN'e bakın" denirken EXPLAIN'in gerçekten alınabildiği kontrol edilmeli;
alınamıyorsa boş yönlendirme yerine "şu yüzden öneremiyorum" denmeli.

Ayrıca sınırlılık bilgileri ("darboğaz belirlenemedi çünkü şu veri yok") bulgu metninin içine
gömülü uzun bir cümle olarak değil, ayrı ve kısa bir not olarak taşınmalı.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import explain_feasibility, performance_section


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _at(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


async def _instance(session) -> Instance:
    instance = Instance(
        name=f"adv-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


async def _seed(session, instance_id: int, text: str) -> None:
    session.add(SlowQuerySample(
        instance_id=instance_id, collected_at=_at(90), queryid="q", query=text,
        calls=0, total_time_ms=0, mean_time_ms=0,
    ))
    session.add(SlowQuerySample(
        instance_id=instance_id, collected_at=_at(5), queryid="q", query=text,
        calls=40, total_time_ms=8_000, mean_time_ms=200,
        shared_blks_read=900, shared_blks_hit=100,
    ))
    await session.commit()


def _ctx(session, instances) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(hours=3), period_end=end, previous=None, previous_findings={},
    )


# --- EXPLAIN uygulanabilirliği ------------------------------------------------------------


def test_select_queries_are_explainable():
    ok, reason = explain_feasibility("SELECT * FROM orders WHERE customer_id = $1")
    assert ok and reason is None


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE invoices SET paid = true WHERE id = $1",
        "INSERT INTO events (name) VALUES ($1)",
        "VACUUM ANALYZE orders",
        "SELECT 1; SELECT 2",
    ],
)
def test_non_explainable_queries_are_detected_with_a_reason(query: str):
    ok, reason = explain_feasibility(query)
    assert not ok
    assert reason, "neden açıklanmalı"


# --- Öneri geçerliliği ----------------------------------------------------------------------


async def test_advice_points_at_explain_only_when_explain_actually_works():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed(session, instance.id, "SELECT * FROM orders WHERE customer_id = $1")
        result = await performance_section(_ctx(session, [instance]))

    finding = result.findings[0]
    assert finding.evidence["explainable"] is True
    assert "plan" in finding.advice.title.lower()
    # Yönlendirme adımı var ve EXPLAIN'den bahsediyor.
    assert any("EXPLAIN" in (s.action or "") for s in finding.advice.steps)


async def test_advice_explains_why_it_cannot_advise_for_a_dml_query():
    """Boş yönlendirme yok: "EXPLAIN'e bakın" yerine NEDEN bakılamayacağı yazılmalı."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed(session, instance.id, "UPDATE invoices SET paid = true WHERE id = $1")
        result = await performance_section(_ctx(session, [instance]))

    finding = result.findings[0]
    assert finding.evidence["explainable"] is False
    assert "yapılamıyor" in finding.advice.title
    assert "EXPLAIN alınamıyor" in finding.advice.why
    # Yine de uygulanabilir bir alternatif adım sunulmalı — öneri boş kalmamalı.
    assert finding.advice.steps
    assert finding.advice.unavailable_reason is None, "alternatif varken 'öneri yok' denmemeli"
    # Ve DPA'da EXPLAIN'e yönlendiren bir cümle KALMAMALI.
    assert "EXPLAIN planına" not in (finding.recommendation or "")


async def test_limitation_is_a_short_separate_note_not_buried_in_the_detail():
    """Bildirilen istek: "darboğaz belirlenemedi çünkü şu veri yok" ayrı ve kısa görünsün."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        # exec_user_time/exec_sys_time yok → darboğaz sınıfı "kesin değil".
        session.add(SlowQuerySample(
            instance_id=instance.id, collected_at=_at(90), queryid="u", query="SELECT * FROM t WHERE a = $1",
            calls=0, total_time_ms=0, mean_time_ms=0,
        ))
        session.add(SlowQuerySample(
            instance_id=instance.id, collected_at=_at(5), queryid="u", query="SELECT * FROM t WHERE a = $1",
            calls=40, total_time_ms=8_000, mean_time_ms=200,
            shared_blks_read=10, shared_blks_hit=1000,
        ))
        await session.commit()
        result = await performance_section(_ctx(session, [instance]))

    finding = result.findings[0]
    assert finding.note, "sınırlılık notu ayrı alanda olmalı"
    assert "kesin değil" in finding.note
    # Uzun açıklama artık detay metnine gömülü DEĞİL.
    assert "desteklemiyor olabilir" not in finding.detail
    # Detay metni kısa ve yapılandırılmış kalmalı.
    assert len(finding.detail) < 240


async def test_snapshot_mode_limitation_is_also_a_note():
    """Tek döngülük pencerede "değerler kümülatif" uyarısı da nota gider, cümleye değil."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(SlowQuerySample(
            instance_id=instance.id, collected_at=_at(5), queryid="s", query="SELECT * FROM t WHERE a = $1",
            calls=40, total_time_ms=8_000, mean_time_ms=200,
        ))
        await session.commit()
        result = await performance_section(_ctx(session, [instance]))

    finding = result.findings[0]
    assert "kümülatif" in (finding.note or "")
    assert "kümülatif" not in finding.detail


async def test_every_finding_with_a_link_also_has_actionable_advice():
    """Bir sayfaya yönlendiren her bulgu, orada ne yapılacağını da söylemeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed(session, instance.id, "SELECT * FROM orders WHERE customer_id = $1")
        result = await performance_section(_ctx(session, [instance]))

    for finding in result.findings:
        if not finding.link_hint:
            continue
        assert finding.advice is not None, f"{finding.title}: yönlendirme var ama öneri yok"
        assert finding.advice.steps or finding.advice.unavailable_reason
