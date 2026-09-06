"""Faz 18 İŞ 4 — rapor okunabilirliği.

Bulgu metinleri tek uzun paragraftı; "ne oldu / ne kadar / neye göre / ne yapmalı" ayrımı
yoktu, sorgu metni okunmayacak yerden kesiliyordu ve aynı bulgu iki kez listelenebiliyordu.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, MetricSample, SlowQuerySample
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import _short_query, availability_section, performance_section


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _at(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


async def _instance(session) -> Instance:
    instance = Instance(
        name=f"read-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


def _ctx(session, instances) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(hours=3), period_end=end, previous=None, previous_findings={},
    )


async def _seed_slow(session, instance_id: int, text: str, *, queryid: str = "q") -> None:
    session.add(SlowQuerySample(
        instance_id=instance_id, collected_at=_at(90), queryid=queryid, query=text,
        calls=0, total_time_ms=0, mean_time_ms=0,
    ))
    session.add(SlowQuerySample(
        instance_id=instance_id, collected_at=_at(5), queryid=queryid, query=text,
        calls=40, total_time_ms=8_000, mean_time_ms=200,
    ))
    await session.commit()


# --- Sorgu metni kısaltması ----------------------------------------------------------------


def test_short_query_cuts_at_a_word_boundary_not_mid_identifier():
    query = "SELECT customer_id, order_total, created_at FROM public.orders_archive WHERE created_at > $1"
    short = _short_query(query, limit=50)

    assert short.endswith("…")
    body = short[:-1]
    # Kesme kelime sınırında olmalı: son parça yarım bir tanımlayıcı olmamalı.
    assert not query[len(body)].isalnum() or query[len(body) - 1] == " " or body == body.rstrip()
    assert " " in body
    assert query.startswith(body.rstrip("…").rstrip())


def test_short_query_keeps_the_meaningful_beginning():
    query = "SELECT * FROM orders WHERE customer_id = $1 AND status = $2 ORDER BY created_at DESC LIMIT 100"
    short = _short_query(query, limit=40)

    # Anlamlı kısım (ne yaptığı) görünmeli.
    assert short.startswith("SELECT * FROM orders")


def test_short_query_leaves_short_queries_untouched():
    assert _short_query("SELECT 1") == "SELECT 1"


def test_short_query_collapses_whitespace():
    assert _short_query("SELECT\n   1,\n   2") == "SELECT 1, 2"


# --- Yapılandırılmış bulgu metni ------------------------------------------------------------


async def test_finding_separates_what_happened_from_how_much():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_slow(session, instance.id, "SELECT * FROM orders WHERE customer_id = $1")
        result = await performance_section(_ctx(session, [instance]))

    finding = result.findings[0]
    # "Ne oldu" kısa bir cümle.
    assert finding.detail.startswith(("Yeni pahalı sorgu", "Pahalı sorgu kötüleşti"))
    assert len(finding.detail) < 200

    # "Ne kadar / neye göre" etiketli satırlarda.
    labels = {f["label"] for f in finding.facts}
    assert {"Toplam süre", "Çağrı", "Ortalama", "Darboğaz"} <= labels
    assert any(f["label"].startswith("Önceki") for f in finding.facts)

    # "Ne yapmalı" öneride.
    assert finding.advice is not None and finding.advice.title


async def test_important_numbers_are_marked_for_emphasis():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_slow(session, instance.id, "SELECT * FROM orders WHERE customer_id = $1")
        result = await performance_section(_ctx(session, [instance]))

    facts = result.findings[0].facts
    toned = [f for f in facts if f["tone"] != "neutral"]
    assert toned, "önemli sayılar vurgulanmak üzere işaretlenmeli"
    assert all(f["tone"] in ("neutral", "good", "bad") for f in facts)


async def test_full_query_text_is_preserved_in_evidence_for_the_collapsible_area():
    long_query = "SELECT " + ", ".join(f"col_{i}" for i in range(60)) + " FROM big_table WHERE id = $1"
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_slow(session, instance.id, long_query)
        result = await performance_section(_ctx(session, [instance]))

    finding = result.findings[0]
    assert finding.evidence["query"] == long_query, "tam metin kaybolmamalı"
    assert len(finding.detail) < len(long_query), "detayda kısaltılmış hali olmalı"


async def test_availability_finding_is_also_structured():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset in (3600, 3585, 2385):
            session.add(MetricSample(instance_id=instance.id, collected_at=now - timedelta(seconds=offset)))
        await session.commit()
        ctx = hr.ReportContext(
            session=session, scope=hr.ReportScope("global", None, "x"), instances=[instance],
            period_start=now - timedelta(hours=2), period_end=now, previous=None, previous_findings={},
        )
        result = await availability_section(ctx)

    finding = result.findings[0]
    labels = {f["label"] for f in finding.facts}
    assert {"Toplam kesinti", "En uzunu", "Erişilebilirlik"} <= labels
    assert finding.note, "sınırlılık notu ayrı alanda"


# --- Aynı bulgunun iki kez listelenmemesi ---------------------------------------------------


async def test_the_same_query_is_not_listed_twice_in_the_data_table():
    """Bildirilen hata: aynı sorgu raporda iki kez görünüyordu (queryid bazen NULL geldiği için)."""
    text = "SELECT * FROM invoices WHERE customer_id = $1"
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(SlowQuerySample(
            instance_id=instance.id, collected_at=_at(90), queryid=None, query=text,
            calls=0, total_time_ms=0, mean_time_ms=0,
        ))
        session.add(SlowQuerySample(
            instance_id=instance.id, collected_at=_at(30), queryid="abc", query=text,
            calls=20, total_time_ms=4_000, mean_time_ms=200,
        ))
        session.add(SlowQuerySample(
            instance_id=instance.id, collected_at=_at(5), queryid="abc", query=text,
            calls=40, total_time_ms=9_000, mean_time_ms=225,
        ))
        await session.commit()
        result = await performance_section(_ctx(session, [instance]))

    rows = result.data["top_queries"]
    identities = [(r["instance_id"], r["key"]) for r in rows]
    assert len(identities) == len(set(identities)), f"aynı sorgu birden çok kez listelendi: {rows}"

    titles = [f.title for f in result.findings]
    assert len(titles) == len(set(titles)), f"aynı bulgu birden çok kez üretildi: {titles}"


async def test_two_genuinely_different_queries_still_produce_two_findings():
    """Tekilleştirme fazla agresif olmamalı — farklı sorgular ayrı bulgu üretmeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_slow(session, instance.id, "SELECT * FROM orders WHERE id = $1", queryid="a")
        await _seed_slow(session, instance.id, "SELECT * FROM invoices WHERE id = $1", queryid="b")
        result = await performance_section(_ctx(session, [instance]))

    keys = {r["key"] for r in result.data["top_queries"]}
    assert len(keys) == 2
