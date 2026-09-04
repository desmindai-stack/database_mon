"""Sağlık Raporu uçları (Faz 17 İŞ 1).

Yetki: router main.py'de `require_write_access` ile bağlanıyor — yani viewer rolü raporları
GÖREBİLİR (GET) ama elle tetikleyemez ve bulgu kabul edemez (POST/DELETE). Faz 17 İŞ 5'te
istenen davranış bu; ayrıca bir kontrol eklemeye gerek yok.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.scheduler import reschedule_health_report
from app.database import get_db
from app.models import FindingAcknowledgement, HealthReport, ReportFinding, User
from app.schemas import (
    AcknowledgeFindingRequest,
    ExecutiveReportOut,
    FindingAcknowledgementOut,
    HealthReportOut,
    HealthReportScheduleOut,
    HealthReportScheduleUpdate,
    HealthReportSummaryOut,
    ReportFindingOut,
    RunReportRequest,
)
from app.services.auth_deps import get_current_user
from app.services.executive_report import build_executive_report
from app.services.report_documents import (
    EXECUTIVE_SECTION_KEYS,
    build_executive_document,
    build_technical_document,
)
from app.services.report_export import RENDERERS, export_filename
from app.services.health_report import ReportScope, enqueue_report, resolve_scope_label
from app.services.settings import get_health_report_schedule, set_health_report_schedule

# report_sections modülünü import etmek bölüm builder'larını kaydeder (register_section
# dekoratörü import anında çalışır) — bu satır olmadan raporlar bölümsüz üretilirdi.
from app.services import report_sections  # noqa: F401

router = APIRouter(prefix="/reports", tags=["reports"])


async def _counts(db: AsyncSession, report_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Rapor başına (kritik, uyarı) sayısı — kabul edilmiş ve kapanmış bulgular hariç.

    Kabul edilen bulgular kritik sayısını şişirmemeli (Faz 17 İŞ 1 gereksinimi); kapanmış
    (resolved) bulgular da zaten sorun değil.
    """
    if not report_ids:
        return {}
    rows = (
        await db.execute(
            select(ReportFinding.report_id, ReportFinding.severity, func.count())
            .where(
                ReportFinding.report_id.in_(report_ids),
                ReportFinding.acknowledged.is_(False),
                ReportFinding.change_state != "resolved",
            )
            .group_by(ReportFinding.report_id, ReportFinding.severity)
        )
    ).all()
    out: dict[int, tuple[int, int]] = {}
    for report_id, severity, count in rows:
        critical, warning = out.get(report_id, (0, 0))
        if severity == "critical":
            critical += count
        elif severity == "warning":
            warning += count
        out[report_id] = (critical, warning)
    return out


def _summary(report: HealthReport, counts: dict[int, tuple[int, int]]) -> HealthReportSummaryOut:
    critical, warning = counts.get(report.id, (0, 0))
    out = HealthReportSummaryOut.model_validate(report)
    out.critical_count = critical
    out.warning_count = warning
    return out


@router.get("", response_model=list[HealthReportSummaryOut])
async def list_reports(
    scope_type: str | None = Query(default=None),
    scope_id: int | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[HealthReportSummaryOut]:
    stmt = select(HealthReport).order_by(HealthReport.generated_at.desc()).limit(limit)
    if scope_type:
        stmt = stmt.where(HealthReport.scope_type == scope_type)
    if scope_id is not None:
        stmt = stmt.where(HealthReport.scope_id == scope_id)
    reports = list((await db.execute(stmt)).scalars().all())
    counts = await _counts(db, [r.id for r in reports])
    return [_summary(r, counts) for r in reports]


@router.get("/latest", response_model=HealthReportSummaryOut | None)
async def latest_report(
    scope_type: str = Query(default="global"),
    scope_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> HealthReportSummaryOut | None:
    """Dashboard'daki "bugünün raporu" kartı için (Faz 17 İŞ 5)."""
    stmt = (
        select(HealthReport)
        .where(HealthReport.scope_type == scope_type, HealthReport.status == "done")
        .order_by(HealthReport.generated_at.desc())
        .limit(1)
    )
    stmt = stmt.where(HealthReport.scope_id.is_(None) if scope_id is None else HealthReport.scope_id == scope_id)
    report = (await db.execute(stmt)).scalar_one_or_none()
    if report is None:
        return None
    counts = await _counts(db, [report.id])
    return _summary(report, counts)


@router.get("/schedule", response_model=HealthReportScheduleOut)
async def get_schedule(db: AsyncSession = Depends(get_db)) -> HealthReportScheduleOut:
    return HealthReportScheduleOut(**await get_health_report_schedule(db))


@router.put("/schedule", response_model=HealthReportScheduleOut)
async def update_schedule(
    payload: HealthReportScheduleUpdate, db: AsyncSession = Depends(get_db)
) -> HealthReportScheduleOut:
    try:
        updated = await set_health_report_schedule(
            db, hour=payload.hour, enabled=payload.enabled, scope_mode=payload.scope_mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reschedule_health_report(updated["hour"])
    return HealthReportScheduleOut(**updated)


@router.get("/acknowledgements", response_model=list[FindingAcknowledgementOut])
async def list_acknowledgements(db: AsyncSession = Depends(get_db)) -> list[FindingAcknowledgement]:
    rows = (
        await db.execute(select(FindingAcknowledgement).order_by(FindingAcknowledgement.acknowledged_at.desc()))
    ).scalars().all()
    return list(rows)


@router.post("/acknowledgements", response_model=FindingAcknowledgementOut, status_code=201)
async def acknowledge_finding(
    payload: AcknowledgeFindingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FindingAcknowledgement:
    """Bulguyu "bilinen konu" olarak işaretler.

    Aynı fingerprint+kapsam için ikinci bir kayıt açılmaz, var olan güncellenir (idempotent) —
    aksi halde unique kısıt hatası alınır ve arayüzün "süreyi uzat" akışı çalışmazdı.
    """
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days) if payload.expires_in_days else None
    )
    existing = (
        await db.execute(
            select(FindingAcknowledgement).where(
                FindingAcknowledgement.fingerprint == payload.fingerprint,
                FindingAcknowledgement.scope_type == payload.scope_type,
                FindingAcknowledgement.scope_id.is_(None)
                if payload.scope_id is None
                else FindingAcknowledgement.scope_id == payload.scope_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = FindingAcknowledgement(
            fingerprint=payload.fingerprint,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
        )
        db.add(existing)
    existing.acknowledged_by = user.username
    existing.acknowledged_at = datetime.now(UTC)
    existing.expires_at = expires_at
    existing.note = payload.note
    await db.commit()
    await db.refresh(existing)

    # Açık raporlardaki aynı bulguyu da hemen işaretle — kullanıcı "Kabul et" dedikten sonra
    # raporu yenilemeden sonucu görsün.
    await db.execute(
        ReportFinding.__table__.update()
        .where(ReportFinding.fingerprint == payload.fingerprint)
        .values(acknowledged=True)
    )
    await db.commit()
    return existing


@router.delete("/acknowledgements/{ack_id}", status_code=204)
async def remove_acknowledgement(ack_id: int, db: AsyncSession = Depends(get_db)) -> None:
    row = await db.get(FindingAcknowledgement, ack_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Kabul kaydı bulunamadı")
    fingerprint = row.fingerprint
    await db.delete(row)
    await db.execute(
        ReportFinding.__table__.update()
        .where(ReportFinding.fingerprint == fingerprint)
        .values(acknowledged=False)
    )
    await db.commit()


@router.post("/run", response_model=HealthReportSummaryOut, status_code=202)
async def run_report(payload: RunReportRequest, db: AsyncSession = Depends(get_db)) -> HealthReportSummaryOut:
    """Raporu elle tetikler. Arka planda üretilir; dönen satır "queued" durumundadır."""
    if payload.scope_type != "global" and payload.scope_id is None:
        raise HTTPException(status_code=400, detail="Bu kapsam için scope_id zorunlu")

    period_end = payload.period_end or datetime.now(UTC)
    period_start = payload.period_start or (period_end - timedelta(days=payload.period_days))
    if period_start >= period_end:
        raise HTTPException(status_code=400, detail="Dönem başlangıcı bitişten önce olmalı")

    label = await resolve_scope_label(db, payload.scope_type, payload.scope_id)
    report = await enqueue_report(
        ReportScope(payload.scope_type, payload.scope_id, label), period_start, period_end, generated_by="manual"
    )
    return _summary(report, {})


@router.get("/{report_id}", response_model=HealthReportOut)
async def get_report(report_id: int, db: AsyncSession = Depends(get_db)) -> HealthReportOut:
    report = await db.get(HealthReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")

    findings = list(
        (
            await db.execute(
                select(ReportFinding)
                .where(ReportFinding.report_id == report_id)
                # Etki × aciliyet sıralaması (Faz 17 İŞ 6) — alfabetik değil.
                .order_by(ReportFinding.priority.desc(), ReportFinding.title.asc())
            )
        ).scalars().all()
    )
    counts = await _counts(db, [report_id])
    # HealthReportOut'u doğrudan ORM nesnesinden doğrulamıyoruz: `findings` bir ilişki alanı ve
    # async oturumda lazy-load denemesi MissingGreenlet hatası verir. Özet alanları hafif
    # şemadan alıp bulguları elle yerleştiriyoruz.
    summary = _summary(report, counts)
    return HealthReportOut(
        **summary.model_dump(),
        sections=report.sections or {},
        findings=[ReportFindingOut.model_validate(f) for f in findings],
    )


@router.get("/{report_id}/executive", response_model=ExecutiveReportOut)
async def get_executive_report(report_id: int, db: AsyncSession = Depends(get_db)) -> ExecutiveReportOut:
    """Aynı raporun yönetici (müşteri) görünümü — teknik detay içermez."""
    report = await db.get(HealthReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")
    if report.status != "done":
        raise HTTPException(status_code=409, detail="Rapor henüz tamamlanmadı")
    executive = await build_executive_report(db, report)
    return ExecutiveReportOut(**vars(executive))


@router.get("/{report_id}/export")
async def export_report(
    report_id: int,
    format: str = Query(default="pdf", pattern="^(pdf|html|md)$"),
    view: str = Query(default="technical", pattern="^(technical|executive)$"),
    sections: str | None = Query(
        default=None,
        description="Virgülle ayrılmış bölüm anahtarları. Verilmezse tüm bölümler dahil edilir.",
    ),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Raporu PDF/HTML/Markdown olarak indirir (Faz 17 İŞ 4).

    GET olduğu için viewer rolü de dışa aktarabilir — istenen davranış bu. `sections` ile
    müşteriye gönderilecek çıktıdan teknik bölümler çıkarılabilir.
    """
    report = await db.get(HealthReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")
    if report.status != "done":
        raise HTTPException(status_code=409, detail="Rapor henüz tamamlanmadı")

    selected = [s.strip() for s in sections.split(",") if s.strip()] if sections else None

    if view == "executive":
        executive = await build_executive_report(db, report)
        document = build_executive_document(executive, selected)
    else:
        findings = list(
            (
                await db.execute(
                    select(ReportFinding)
                    .where(ReportFinding.report_id == report_id)
                    .order_by(ReportFinding.priority.desc())
                )
            ).scalars().all()
        )
        document = build_technical_document(report, findings, selected)

    render, media_type, extension = RENDERERS[format]
    payload = render(document)
    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    filename = export_filename(
        report.scope_label, report.generated_at, extension, view="yonetici" if view == "executive" else ""
    )
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{report_id}/export-sections", response_model=list[dict])
async def list_exportable_sections(report_id: int, view: str = Query(default="technical"), db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Dışa aktarma öncesi bölüm seçimi için kullanılabilir bölümlerin listesi."""
    report = await db.get(HealthReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")
    if view == "executive":
        labels = {
            "summary": "Genel değerlendirme",
            "availability": "Erişilebilirlik",
            "inventory": "Sistem envanteri",
            "risks": "Risk özeti",
            "trend": "Önceki döneme göre",
            "work_done": "Bu dönemde yapılanlar",
            "recommendations": "Öneriler",
        }
        return [{"key": k, "title": labels[k]} for k in EXECUTIVE_SECTION_KEYS]
    sections = report.sections or {}
    items = sections.get("items") or {}
    return [
        {"key": key, "title": (items.get(key) or {}).get("title") or key}
        for key in (sections.get("order") or [])
    ]


@router.delete("/{report_id}", status_code=204)
async def delete_report(report_id: int, db: AsyncSession = Depends(get_db)) -> None:
    report = await db.get(HealthReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")
    # Bu raporu "önceki" olarak gösteren kayıtların bağını kopar — aksi halde foreign key
    # kısıtı silmeyi engeller (aynı tabloya kendi kendine referans).
    await db.execute(
        HealthReport.__table__.update()
        .where(HealthReport.previous_report_id == report_id)
        .values(previous_report_id=None)
    )
    await db.execute(delete(ReportFinding).where(ReportFinding.report_id == report_id))
    await db.delete(report)
    await db.commit()
