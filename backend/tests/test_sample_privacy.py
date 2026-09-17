"""Gerçek değerli sorgu metninin nereye gidebildiği (Faz 31 İŞ 2).

Bekleme örnekleyicisi sorgu metnini pg_stat_activity'den okuyor; uygulama değerleri metne
gömüyorsa metin gerçek veri (kimlik no, e-posta, tutar) taşıyor. Kural:

1. Gerçek değer YALNIZCA `WaitQuerySignature.sample_*` alanlarında ve yalnızca ayar açıkken.
2. Bu alanı okuyabilen modüller sınırlı; rapor, yönetici raporu ve gösterge paneli modülleri
   bu alanlara ERİŞEMEZ.
3. `query_text` (yük kırılımında ve teknik raporda görünen alan) her zaman değersiz.
4. Ayar kapatılınca saklanmış örnekler silinir; yükseltmede eski ham metin arındırılır.

Canlı kanıtlar (gerçek örnekleyici, gerçek sunucu): `test_plan_source_live_postgres.py`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Instance, WaitQuerySignature
from app.services.analysis_settings import get_analysis_settings, set_analysis_settings
from app.services.credentials import encrypt_secret
from app.services.wait_sampling import enforce_query_text_privacy

APP = Path(__file__).resolve().parents[1] / "app"

#: `sample_query_text` alanına dokunabilen modüller — ve NEDEN.
ALLOWED = {
    "models.py": "tanım",
    "database.py": "yerel SQLite'ta kolonun eklenmesi (migrate_schema) — veri okumuyor",
    "services/wait_sampling.py": "yazma ve silme",
    "services/query_text_privacy.py": "geriye dönük temizlik: yardımcı ifade/PASSWORD arındırması (metin yanıta konmuyor)",
    "services/plan_source.py": "varlığını ve çalıştırılabilirliğini değerlendirme (metin yanıta konmuyor)",
    "routers/queries.py": "admin'in POST ettiği EXPLAIN ANALYZE çağrısına girdi",
}

#: Rapor ve yönetici görünümünü üreten modüller — gerçek değerli örnekle HİÇBİR ilişkileri olmamalı.
REPORT_MODULES = [
    "services/health_report.py",
    "services/executive_report.py",
    "services/report_sections.py",
    "services/report_documents.py",
    "services/report_export.py",
    "services/dashboard.py",
    "services/dashboard_snapshot.py",
    "services/database_load.py",
]


def _py_files():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_allowlisted_modules_touch_the_real_valued_sample():
    touching = {
        p.relative_to(APP).as_posix()
        for p in _py_files()
        if "sample_query_text" in p.read_text(encoding="utf-8")
    }
    unexpected = touching - set(ALLOWED)
    assert not unexpected, f"gerçek değerli örneğe izinsiz erişim: {sorted(unexpected)}"
    assert touching == set(ALLOWED), "izin listesi güncel değil (erişim kaldırıldıysa listeden de çıkarın)"


@pytest.mark.parametrize("module", REPORT_MODULES)
def test_report_and_dashboard_modules_never_see_samples(module):
    source = (APP / module).read_text(encoding="utf-8")
    for field in ("sample_query_text", "sample_duration_ms", "sample_captured_at"):
        assert field not in source, f"{module} → {field}"


def test_plan_sources_response_schema_has_no_query_text_field():
    """Plan kaynakları GET ucu viewer'a açık — yanıt şemasında metin alanı olmamalı."""
    from app.schemas import PlanSourceOptionOut, PlanSourcesOut

    fields = set(PlanSourcesOut.model_fields) | set(PlanSourceOptionOut.model_fields)
    assert not {f for f in fields if "query" in f and f != "queryid"}


# --- Temizlik — gerçek (SQLite) veritabanı ---------------------------------------------------


async def _signature(raw_text: str, sample: str | None) -> tuple[int, int]:
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"privacy-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
            database="d", username="u", password=encrypt_secret("x"),
        )
        session.add(instance)
        await session.flush()
        row = WaitQuerySignature(
            instance_id=instance.id, queryid=uuid.uuid4().hex[:12], query_text=raw_text,
            sample_query_text=sample, sample_duration_ms=1200.0 if sample else None,
        )
        session.add(row)
        await session.commit()
        return instance.id, row.id


async def _load(row_id: int) -> WaitQuerySignature:
    async with SessionLocal() as session:
        return (await session.execute(select(WaitQuerySignature).where(WaitQuerySignature.id == row_id))).scalar_one()


async def test_default_is_off():
    await init_db()
    async with SessionLocal() as session:
        await set_analysis_settings(session, store_real_query_samples=False)
        assert (await get_analysis_settings(session))["store_real_query_samples"] is False


async def test_upgrade_cleanup_strips_values_from_pre_existing_text_and_drops_samples_when_off():
    """Faz 31 öncesinde query_text her zaman ham metindi — ilk yazımda arındırılıyor."""
    raw = "SELECT * FROM customers WHERE tc_no = '12345678901' AND balance > 1500.75"
    _, row_id = await _signature(raw, sample=raw)
    async with SessionLocal() as session:
        await set_analysis_settings(session, store_real_query_samples=False)
        await enforce_query_text_privacy(session)
    row = await _load(row_id)
    assert "12345678901" not in row.query_text and "1500.75" not in row.query_text
    assert row.sample_query_text is None and row.sample_duration_ms is None


async def test_when_on_samples_are_kept_but_query_text_is_still_valueless():
    raw = "SELECT * FROM customers WHERE email = 'ayse@example.com'"
    _, row_id = await _signature(raw, sample=raw)
    async with SessionLocal() as session:
        await set_analysis_settings(session, store_real_query_samples=True)
        await enforce_query_text_privacy(session)
    row = await _load(row_id)
    try:
        assert row.sample_query_text == raw
        assert "ayse@example.com" not in row.query_text
    finally:
        async with SessionLocal() as session:
            await set_analysis_settings(session, store_real_query_samples=False)


async def test_switching_off_deletes_samples_immediately():
    raw = "SELECT * FROM customers WHERE iban = 'TR000000000000000000000000'"
    async with SessionLocal() as session:
        await init_db()
        await set_analysis_settings(session, store_real_query_samples=True)
    _, row_id = await _signature(raw, sample=raw)
    async with SessionLocal() as session:
        await set_analysis_settings(session, store_real_query_samples=False)
    assert (await _load(row_id)).sample_query_text is None
