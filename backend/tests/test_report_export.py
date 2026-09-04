"""Faz 17 İŞ 4 — rapor dışa aktarma (PDF / HTML / Markdown).

Kanıtlananlar: üç biçim de aynı içerik belgesinden üretiliyor, bölüm seçimi çalışıyor
(müşteriye gidecek çıktıdan teknik bölümler çıkarılabiliyor), dosya adı anlamlı, PDF gerçek
bir PDF ve Türkçe karakterleri basabiliyor, yönetici çıktısına teknik detay sızmıyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import HealthReport, ReportFinding
from app.services.report_documents import build_technical_document
from app.services.report_export import (
    Document,
    bullets,
    code,
    export_filename,
    heading,
    keyvalues,
    note,
    paragraph,
    render_html,
    render_markdown,
    render_pdf,
    slugify,
    table,
)
from tests.auth_helper import authed_client


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _sample_document() -> Document:
    doc = Document(
        title="X Bank — Teknik Sağlık Raporu",
        subtitle="Günlük denetim",
        meta=[("Dönem", "03.09.2026 – 04.09.2026"), ("Genel durum", "Kritik")],
    )
    doc.blocks += [
        heading("Erişilebilirlik", 2),
        paragraph("Şu ana kadar üç kesinti görüldü — ölçüm toplama boşluklarından türetildi."),
        bullets(["Birinci düğüm çevrimdışı kaldı", "İkinci düğüm sağlıklı"]),
        table(["Veritabanı", "Erişilebilirlik"], [["boa-prod", "%99.1"], ["aapara-prod", "%100"]]),
        keyvalues([("Kesinti sayısı", "3"), ("En uzun", "20 dk")]),
        note("Öneri: kesinti saatlerindeki logları inceleyin.", "warning"),
        code("VACUUM (ANALYZE) \"app\".\"orders\";"),
    ]
    return doc


# --- Renderer'lar ------------------------------------------------------------------------


def test_markdown_contains_every_block_type():
    output = render_markdown(_sample_document())

    assert output.startswith("# X Bank — Teknik Sağlık Raporu")
    assert "## Erişilebilirlik" in output
    assert "- Birinci düğüm çevrimdışı kaldı" in output
    assert "| Veritabanı | Erişilebilirlik |" in output
    assert "| boa-prod | %99.1 |" in output
    assert "**Kesinti sayısı:** 3" in output
    assert "> Öneri:" in output
    assert "```sql" in output


def test_html_is_self_contained_and_escapes_content():
    doc = _sample_document()
    doc.blocks.append(paragraph("<script>alert(1)</script> & tehlike"))
    output = render_html(doc)

    assert output.startswith("<!doctype html>")
    assert "<style>" in output, "stil gömülü olmalı — dosya tek başına açılabilsin"
    assert "<script>alert(1)</script>" not in output
    assert "&lt;script&gt;" in output
    assert "&amp; tehlike" in output
    # Yazdırma için sayfa kırılma kuralları var.
    assert "@media print" in output


def test_pdf_is_a_real_pdf_and_embeds_turkish_glyphs():
    payload = render_pdf(_sample_document())

    assert payload.startswith(b"%PDF-"), "geçerli bir PDF üretilmeli"
    assert payload.rstrip().endswith(b"%%EOF")
    assert len(payload) > 2000
    # Türkçe karakterler gömülü fontla basılıyor: font adı PDF içinde görünmeli.
    assert b"Vera" in payload or b"DbaceSans" in payload


def test_pdf_handles_turkish_characters_without_error():
    """Varsayılan Helvetica ğ/ş/ı basamaz; gömülü font bu yüzden şart."""
    doc = Document(title="Şirket Değerlendirmesi", subtitle="Ölçüm ışığında", meta=[])
    doc.blocks = [paragraph("Iğdır'da çalışan sunucuların şifrelenmiş bağlantıları güncellendi.")]

    payload = render_pdf(doc)

    assert payload.startswith(b"%PDF-")


def test_empty_table_is_skipped_in_all_formats():
    doc = Document(title="Boş", subtitle="", meta=[])
    doc.blocks = [table(["A", "B"], [])]

    assert "| A | B |" not in render_markdown(doc)
    assert "<table>" not in render_html(doc)
    assert render_pdf(doc).startswith(b"%PDF-")


# --- Dosya adı ---------------------------------------------------------------------------


def test_filename_is_meaningful_and_ascii_safe():
    at = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)

    assert export_filename("X Bank Şubesi", at, "pdf") == "x-bank-subesi_rapor_2026-09-04.pdf"
    assert export_filename("X Bank", at, "pdf", view="yonetici") == "x-bank_rapor-yonetici_2026-09-04.pdf"
    assert export_filename("", at, "md") == "rapor_2026-09-04.md"


def test_slugify_maps_turkish_letters_instead_of_dropping_them():
    # NFKD 'ı' ve 'ğ' harflerini tamamen düşürür; elle eşleme olmadan "ıgdır" → "gdr" olurdu.
    assert slugify("Iğdır Şube") == "igdir-sube"
    assert slugify("ÇÖZÜM") == "cozum"


# --- Belge üretimi ve bölüm seçimi -------------------------------------------------------


async def _seed(scope_label: str = "X Bank") -> int:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        report = HealthReport(
            scope_type="customer",
            scope_id=uuid.uuid4().int % 1_000_000,
            scope_label=scope_label,
            period_start=now - timedelta(days=1),
            period_end=now,
            generated_by="schedule",
            overall_status="critical",
            status="done",
            progress_pct=100,
            sections={
                "order": ["executive_summary", "availability", "performance"],
                "items": {
                    "executive_summary": {"title": "Yönetici özeti", "status": "critical", "summary": "1 kritik",
                                          "data": {"highlights": [{"severity": "critical", "title": "Kritik bulgu"}]}},
                    "availability": {"title": "Erişilebilirlik", "status": "warning", "summary": "1 kesinti",
                                     "data": {"instances": [{"instance": "db-01", "uptime_pct": 99.1,
                                                             "outage_count": 1, "outage_seconds": 600.0,
                                                             "longest_outage_seconds": 600.0}],
                                              "method": "Boşluklardan türetilir."}},
                    "performance": {"title": "Performans", "status": "warning", "summary": "1 yavaş sorgu",
                                    "data": {"top_queries": [{"instance": "db-01", "query": "SELECT * FROM orders",
                                                              "total_time_ms": 5000, "mean_time_ms": 250,
                                                              "calls": 20, "resource": "io", "change": "worse"}]}},
                },
            },
        )
        session.add(report)
        await session.commit()
        session.add(
            ReportFinding(
                report_id=report.id, section="performance", severity="critical",
                title="db-01: kötüleşen pahalı sorgu", detail="Sorgu yavaşladı.",
                evidence={"metric": "total_exec_time", "value": 5000, "threshold": 1000,
                          "measured_at": now.isoformat()},
                recommendation="EXPLAIN planına bakın.",
                commands=["EXPLAIN ANALYZE SELECT 1;"],
                fingerprint=uuid.uuid4().hex[:16], priority=150.0, open_since_days=3,
            )
        )
        await session.commit()
        return report.id


async def test_technical_document_includes_evidence_recommendation_and_commands():
    report_id = await _seed()
    async with SessionLocal() as session:
        report = await session.get(HealthReport, report_id)
        findings = list(
            (await session.execute(
                ReportFinding.__table__.select().where(ReportFinding.report_id == report_id)
            )).mappings()
        )
        rows = [ReportFinding(**dict(f)) for f in findings]
        doc = build_technical_document(report, rows)

    output = render_markdown(doc)
    assert "Kanıt — metrik: total_exec_time · ölçülen: 5000 · eşik: 1000" in output
    assert "Öneri: EXPLAIN planına bakın." in output
    assert "EXPLAIN ANALYZE SELECT 1;" in output
    # Gürültü kontrolü çıktıya da yansıyor.
    assert "3 gündür açık" in output


async def test_section_selection_drops_unwanted_sections():
    report_id = await _seed()
    async with SessionLocal() as session:
        report = await session.get(HealthReport, report_id)
        doc = build_technical_document(report, [], selected=["availability"])

    output = render_markdown(doc)
    assert "Erişilebilirlik" in output
    assert "Performans" not in output
    assert "Yönetici özeti" not in output


# --- Uçlar --------------------------------------------------------------------------------


async def test_export_endpoint_returns_pdf_with_a_download_filename():
    report_id = await _seed("X Bank")
    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report_id}/export?format=pdf&view=technical")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "x-bank_rapor_" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")


async def test_export_endpoint_supports_html_and_markdown():
    report_id = await _seed()
    async with await authed_client() as c:
        html = await c.get(f"/api/reports/{report_id}/export?format=html")
        markdown = await c.get(f"/api/reports/{report_id}/export?format=md")

    assert html.text.startswith("<!doctype html>")
    assert markdown.text.startswith("# X Bank")
    assert "markdown" in markdown.headers["content-type"]


async def test_export_endpoint_section_filter():
    report_id = await _seed()
    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report_id}/export?format=md&sections=availability")

    assert "Erişilebilirlik" in response.text
    assert "Performans" not in response.text


async def test_executive_export_never_contains_technical_details():
    report_id = await _seed()
    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report_id}/export?format=md&view=executive")

    body = response.text
    for forbidden in ("SELECT", "EXPLAIN", "total_exec_time", "db-01"):
        assert forbidden not in body, f"yönetici çıktısına sızdı: {forbidden}"
    assert "yonetici" in response.headers["content-disposition"]


async def test_export_lists_available_sections_for_the_picker():
    report_id = await _seed()
    async with await authed_client() as c:
        technical = (await c.get(f"/api/reports/{report_id}/export-sections")).json()
        executive = (await c.get(f"/api/reports/{report_id}/export-sections?view=executive")).json()

    assert [s["key"] for s in technical] == ["executive_summary", "availability", "performance"]
    assert [s["title"] for s in technical][1] == "Erişilebilirlik"
    assert {s["key"] for s in executive} >= {"risks", "recommendations"}


async def test_viewer_can_export():
    report_id = await _seed()
    async with await authed_client(role="viewer") as c:
        response = await c.get(f"/api/reports/{report_id}/export?format=pdf")

    assert response.status_code == 200


async def test_export_refuses_unfinished_report():
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        report = HealthReport(
            scope_type="global", scope_id=None, scope_label="Tüm sistem",
            period_start=now - timedelta(days=1), period_end=now, status="running",
        )
        session.add(report)
        await session.commit()
        report_id = report.id

    async with await authed_client() as c:
        assert (await c.get(f"/api/reports/{report_id}/export")).status_code == 409


async def test_export_rejects_unknown_format():
    report_id = await _seed()
    async with await authed_client() as c:
        assert (await c.get(f"/api/reports/{report_id}/export?format=docx")).status_code == 422
