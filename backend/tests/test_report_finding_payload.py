"""GERİLEME: rapor bulgu detayı açılmıyordu (Faz 20 düzeltmesi).

Canlıdaki hata: `TypeError: Cannot read properties of undefined (reading 'length')` — bir
bulgunun detayına tıklayınca.

**Kök neden şu değildi:** "eski kayıtlarda alan yok". Alan HİÇBİR kayıtta dönmüyordu.
`ReportFinding` modelinde `facts` ve `note` kolonları vardı, rapor motoru ikisini de yazıyordu,
ama `ReportFindingOut` şemasında bu iki alan HİÇ TANIMLI DEĞİLDİ. Pydantic tanımsız alanı
sessizce kırptığı için API bunları asla döndürmedi; arayüz `finding.facts.length` okuyunca
`undefined` üzerinden patladı. Yani Faz 18'in iki özelliği (sayısal özet ve sınırlılık notu)
üretiliyor, saklanıyor, ama hiç görünmüyordu.

Testler üç katmanı da kapatıyor:
  1. Şema sürüklenmesi — modeldeki her kolon şemada karşılığını bulmalı.
  2. Değer taşınması — yeni kayıtta alanlar dolu gelmeli.
  3. Eski kayıt — null kolonlar boş dizi/None olarak gelmeli, hata değil.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.database import SessionLocal, init_db
from app.models import HealthReport, ReportFinding
from app.schemas import ReportFindingOut
from tests.auth_helper import authed_client

BACKEND = Path(__file__).resolve().parents[1]

# Şemada karşılığı OLMAMASI beklenen kolonlar: birincil anahtar, yabancı anahtar ve ilişki.
_NOT_EXPOSED = {"id", "report_id", "report"}


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _report_with(session, **finding_fields) -> tuple[HealthReport, ReportFinding]:
    now = datetime.now(UTC)
    report = HealthReport(
        scope_type="global", scope_id=None, scope_label="test",
        period_start=now - timedelta(days=1), period_end=now, generated_at=now,
        generated_by="manual", overall_status="warning", status="done", sections={},
    )
    session.add(report)
    await session.flush()

    base = dict(
        report_id=report.id, section="performance", severity="warning",
        title="Test bulgusu", detail="ayrıntı",
        fingerprint=f"fp-{uuid.uuid4().hex[:10]}", priority=1.0,
        open_since_days=0, change_state="new", acknowledged=False,
        finding_type="test", status="open",
    )
    base.update(finding_fields)
    finding = ReportFinding(**base)
    session.add(finding)
    await session.commit()
    return report, finding


# --- 1. Şema sürüklenmesi — asıl hatayı yakalayan test ---------------------------------------


def test_every_model_column_is_exposed_by_the_schema():
    """HATANIN KENDİSİ: `facts` ve `note` modelde vardı, şemada yoktu; Pydantic sessizce
    kırpıyordu. Bu test o sessiz kırpmayı görünür kılıyor."""
    models_src = (BACKEND / "app" / "models.py").read_text(encoding="utf-8")
    body = re.search(r"class ReportFinding\(Base\):(.*?)\nclass ", models_src, re.S).group(1)
    columns = {m for m in re.findall(r"^    (\w+): Mapped", body, re.M)}

    exposed = set(ReportFindingOut.model_fields)
    missing = sorted(columns - exposed - _NOT_EXPOSED)

    assert not missing, (
        f"ReportFinding modelinde olup şemada olmayan kolonlar: {missing}. "
        "Pydantic bunları sessizce kırpar — API hiç döndürmez, arayüz undefined görür."
    )


def test_facts_and_note_are_part_of_the_schema():
    """Bu iki alan bu gerilemenin konusuydu; adlarıyla kilitleniyor."""
    assert "facts" in ReportFindingOut.model_fields
    assert "note" in ReportFindingOut.model_fields


# --- 2. Yeni kayıt: değerler gerçekten taşınıyor mu ------------------------------------------


async def test_a_new_finding_carries_its_facts_and_note_to_the_api():
    async with SessionLocal() as session:
        report, _ = await _report_with(
            session,
            note="Bu ölçüm günlük fotoğrafa dayanıyor.",
            facts=[
                {"label": "Toplam süre", "value": "1.2 sn", "tone": "bad"},
                {"label": "Çağrı", "value": "340", "tone": "neutral"},
            ],
            commands=["VACUUM ANALYZE public.t;"],
            evidence={"metric": "total_time_ms", "threshold": 1000},
        )

    async with await authed_client() as c:
        body = (await c.get(f"/api/reports/{report.id}")).json()

    finding = body["findings"][0]
    assert finding["note"] == "Bu ölçüm günlük fotoğrafa dayanıyor."
    assert len(finding["facts"]) == 2
    assert finding["facts"][0] == {"label": "Toplam süre", "value": "1.2 sn", "tone": "bad"}
    assert finding["commands"] == ["VACUUM ANALYZE public.t;"]
    assert finding["evidence"]["metric"] == "total_time_ms"


# --- 3. Eski kayıt: null kolonlar arayüzü patlatmamalı ---------------------------------------


async def test_an_old_finding_with_null_columns_returns_empty_collections():
    """Bu alanlar sonradan eklendi; onlardan ÖNCE üretilmiş raporlarda kolonlar NULL.
    İstemci her zaman bir dizi görmeli — `null`/eksik, `.length` okuyan her yeri patlatır."""
    async with SessionLocal() as session:
        report, _ = await _report_with(
            session, note=None, facts=None, commands=None, evidence=None, advice=None,
        )

    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report.id}")

    assert response.status_code == 200, response.text
    finding = response.json()["findings"][0]

    assert finding["facts"] == [], "null facts boş diziye çevrilmeli"
    assert finding["commands"] == [], "null commands boş diziye çevrilmeli"
    assert finding["evidence"] == {}, "null evidence boş nesneye çevrilmeli"
    # Bunlar dizi değil, `null` kalmaları doğru — arayüz falsy kontrolüyle kullanıyor.
    assert finding["note"] is None
    assert finding["advice"] is None


async def test_every_finding_field_the_ui_iterates_is_never_null():
    """Arayüzün üzerinde `.length`/`.map` çağırdığı her alan, eski kayıtta bile dizi/nesne
    olarak gelmeli. Alan adları bileşendeki kullanıma göre seçildi."""
    async with SessionLocal() as session:
        report, _ = await _report_with(session, facts=None, commands=None, evidence=None)

    async with await authed_client() as c:
        finding = (await c.get(f"/api/reports/{report.id}")).json()["findings"][0]

    for field in ("facts", "commands"):
        assert isinstance(finding[field], list), f"{field} dizi değil: {finding[field]!r}"
    assert isinstance(finding["evidence"], dict)


# --- Bozuk/eski biçimli veriye dayanıklılık ---------------------------------------------------


async def test_a_fact_with_an_unknown_tone_does_not_break_the_report():
    """Geçersiz bir ton yüzünden raporun TAMAMI 500 dönmemeli — tek satır sadeleşsin."""
    async with SessionLocal() as session:
        report, _ = await _report_with(
            session, facts=[{"label": "X", "value": "1", "tone": "kirmizi"}]
        )

    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report.id}")

    assert response.status_code == 200, response.text
    assert response.json()["findings"][0]["facts"][0]["tone"] == "neutral"


async def test_a_fact_with_missing_keys_is_filled_in_rather_than_rejected():
    async with SessionLocal() as session:
        report, _ = await _report_with(session, facts=[{"label": "Yalnız etiket"}])

    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report.id}")

    assert response.status_code == 200, response.text
    fact = response.json()["findings"][0]["facts"][0]
    assert fact == {"label": "Yalnız etiket", "value": "", "tone": "neutral"}


async def test_an_advice_with_null_lists_does_not_500_the_whole_report():
    """`advice` serbest biçimli JSON: eski bir kayıtta `steps`/`cautions` null olabilir.
    Doğrulama hatası verirse raporun tamamı 500 döner — bulgu değil, RAPOR kaybedilir."""
    async with SessionLocal() as session:
        report, _ = await _report_with(
            session,
            advice={
                "title": "Bir şey yapın", "why": "sebep",
                "steps": None, "cautions": None,
                "estimated_duration": None, "rollback": None,
                "verification": None, "unavailable_reason": None,
            },
        )

    async with await authed_client() as c:
        response = await c.get(f"/api/reports/{report.id}")

    assert response.status_code == 200, response.text
    advice = response.json()["findings"][0]["advice"]
    assert advice["steps"] == []
    assert advice["cautions"] == []
