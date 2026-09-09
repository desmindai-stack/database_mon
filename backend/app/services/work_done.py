"""Dönemde yapılanlar (Faz 28 İŞ 5).

Müşteriye DBA ekibinin çalıştığını gösteren şey bu. Rapor bugüne kadar yalnızca "şu anda ne
sorun var" diyordu; "bu dönemde ne yapıldı" sorusunun cevabı hiçbir yerde yoktu ve emeğin
görünmemesi, hizmetin değerinin de görünmemesi demek.

## Veri nereden geliyor

İki ayrı kaynak, çünkü iki ayrı olay türü var ve ikisi aynı yere yazılmıyor:

* **Açılan bulgular** — durum geçmişine yazılmaz. Bir bulgu "açıldı" diye bir karar
  verilmez; ilk kez tespit edilir. Bu yüzden dönem içindeki raporların `change_state="new"`
  satırlarından sayılıyor.
* **Kapatılan / planlanan / risk kabul edilen** — bunlar KARAR ve `FindingStatusHistory`'ye
  yazılıyor: kim, ne zaman, hangi durumdan hangisine, hangi notla.

İkisini tek kaynaktan üretmeye çalışmak, ya kararları ya da tespitleri kaybetmek olurdu.

## Tekrar açılanlar neden ayrı

"Çözüldü" denip tekrar tespit edilen bir bulgu, hiç kapatılmamış bir bulgudan farklı bir
şey söylüyor: uygulanan çözüm işe yaramamış. Kapatılanlarla aynı kefeye koymak, ekibin
başarısını olduğundan iyi gösterirdi — raporu satış aracına çevirmenin en kolay yolu.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FindingStatusHistory, HealthReport, Instance, ReportFinding
from app.services.finding_status import (
    STATUS_DEFERRED,
    STATUS_IGNORED,
    STATUS_LABELS_TR,
    STATUS_OPEN,
    STATUS_PLANNED,
    STATUS_RESOLVED,
    STATUS_RESOLVED_PENDING,
    STATUS_RISK_ACCEPTED,
    build_scope_membership,
)

#: "Çözüldü, doğrulanacak" durumundan açığa dönüş de tekrar açılma sayılıyor: ekip kapattı,
#: bulgu yeniden tespit edildi.
_RESOLVED_STATUSES = {STATUS_RESOLVED, STATUS_RESOLVED_PENDING}


async def _scope_keys(session: AsyncSession, instances: list[Instance]) -> set[tuple[str, int | None]]:
    """Bu kapsamı ilgilendiren karar anahtarları.

    Karar müşteri seviyesinde verilmiş olabilir ama rapor bir grup için üretiliyor olabilir;
    yalnızca tam eşleşen kapsamı aramak, verilmiş bir kararı raporda hiç göstermemek olurdu.
    """
    membership = await build_scope_membership(session, instances)
    keys: set[tuple[str, int | None]] = {("global", None)}
    for instance in instances:
        keys.add(("instance", instance.id))
        for kind, mapping in (
            ("group", membership.group_of),
            ("application", membership.application_of),
            ("customer", membership.customer_of),
        ):
            value = mapping.get(instance.id)
            if value is not None:
                keys.add((kind, value))
    return keys


async def collect_work_done(
    session: AsyncSession,
    instances: list[Instance],
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Any]:
    """Dönem içindeki bulgu hareketleri."""
    keys = await _scope_keys(session, instances)

    history = (
        await session.execute(
            select(FindingStatusHistory)
            .where(
                FindingStatusHistory.changed_at >= period_start,
                FindingStatusHistory.changed_at <= period_end,
            )
            .order_by(FindingStatusHistory.changed_at.asc())
        )
    ).scalars().all()
    history = [h for h in history if (h.scope_type, h.scope_id) in keys]

    counts = {
        "closed": 0,
        "planned": 0,
        "risk_accepted": 0,
        "ignored": 0,
        "deferred": 0,
        "reopened": 0,
    }
    events: list[dict[str, Any]] = []
    reopened: list[dict[str, Any]] = []

    for row in history:
        # TEKRAR AÇILMA önce kontrol ediliyor: "çözüldü → açık" geçişi hem bir açılma hem de
        # bir başarısızlık sinyali ve ikinciyi kaybetmemek gerekiyor.
        if row.to_status == STATUS_OPEN and (row.from_status or "") in _RESOLVED_STATUSES:
            counts["reopened"] += 1
            reopened.append(
                {
                    "fingerprint": row.fingerprint,
                    "finding_type": row.finding_type,
                    "changed_at": row.changed_at.isoformat() if row.changed_at else None,
                    "changed_by": row.changed_by or "sistem",
                    "note": row.note,
                }
            )
            continue
        if row.to_status == STATUS_RESOLVED:
            counts["closed"] += 1
        elif row.to_status == STATUS_PLANNED:
            counts["planned"] += 1
        elif row.to_status == STATUS_RISK_ACCEPTED:
            counts["risk_accepted"] += 1
        elif row.to_status == STATUS_IGNORED:
            counts["ignored"] += 1
        elif row.to_status == STATUS_DEFERRED:
            counts["deferred"] += 1
        else:
            continue
        events.append(
            {
                "fingerprint": row.fingerprint,
                "finding_type": row.finding_type,
                "from_status": row.from_status,
                "to_status": row.to_status,
                "status_label": STATUS_LABELS_TR.get(row.to_status, row.to_status),
                "changed_at": row.changed_at.isoformat() if row.changed_at else None,
                "changed_by": row.changed_by or "sistem",
                "note": row.note,
                "reference": row.reference,
            }
        )

    # --- Açılan bulgular ve çözüm süresi: dönem içindeki raporlardan --------------------
    #
    # KAPSAM FİLTRESİ ŞART: kapsam süzülmeseydi bir müşterinin raporu, başka bir müşterinin
    # raporunda açılan bulguları da sayardı — "bu dönemde 40 konu açıldı" cümlesi, o
    # müşteriyle hiç ilgisi olmayan sunuculardan gelirdi.
    report_rows = (
        await session.execute(
            select(HealthReport.id, HealthReport.scope_type, HealthReport.scope_id).where(
                HealthReport.generated_at >= period_start,
                HealthReport.generated_at <= period_end,
                HealthReport.status == "done",
            )
        )
    ).all()
    report_ids = [r.id for r in report_rows if (r.scope_type, r.scope_id) in keys]

    opened = 0
    resolution_days: list[int] = []
    if report_ids:
        rows = (
            await session.execute(
                select(
                    ReportFinding.fingerprint,
                    ReportFinding.change_state,
                    ReportFinding.open_since_days,
                ).where(ReportFinding.report_id.in_(list(report_ids)))
            )
        ).all()
        # Aynı bulgu birden çok raporda "yeni" görünemez ama dönem içinde birden çok rapor
        # üretilmiş olabilir; fingerprint bazında tekilleştiriliyor ki sayı rapor sayısıyla
        # şişmesin.
        new_fingerprints = {r.fingerprint for r in rows if r.change_state == "new"}
        opened = len(new_fingerprints)
        resolution_days = [
            int(r.open_since_days) for r in rows if r.change_state == "resolved"
        ]

    average_resolution_days = (
        round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None
    )

    # Kişi bazında döküm — YALNIZCA teknik rapor için. Yönetici raporunda kim ne yaptı
    # görünmüyor: müşteriye giden bir belgede kişi adı, hizmetin değil bireyin
    # değerlendirilmesine dönüşür.
    by_person: dict[str, int] = {}
    for event in events:
        by_person[event["changed_by"]] = by_person.get(event["changed_by"], 0) + 1

    return {
        "opened": opened,
        **counts,
        "average_resolution_days": average_resolution_days,
        "resolution_sample_size": len(resolution_days),
        "events": events[-100:],
        "reopened_findings": reopened[-50:],
        "by_person": [
            {"person": person, "count": count}
            for person, count in sorted(by_person.items(), key=lambda kv: -kv[1])
        ],
        "measured": bool(report_ids) or bool(history),
    }


def summarize(work: dict[str, Any]) -> str:
    """Tek cümlelik özet — yönetici raporunda da kullanılıyor, o yüzden kişi adı geçmiyor."""
    parts: list[str] = []
    if work.get("opened"):
        parts.append(f"{work['opened']} yeni konu tespit edildi")
    if work.get("closed"):
        parts.append(f"{work['closed']} konu kapatıldı")
    if work.get("planned"):
        parts.append(f"{work['planned']} konu için çalışma planlandı")
    if work.get("risk_accepted"):
        parts.append(f"{work['risk_accepted']} konuda risk bilinçli olarak kabul edildi")
    if work.get("reopened"):
        # Tekrar açılanlar cümlenin SONUNDA ama saklanmıyor: uygulanan çözümün işe
        # yaramadığını gizlemek, raporu satış aracına çevirmek olurdu.
        parts.append(f"{work['reopened']} konu tekrar açıldı")
    if not parts:
        return "Bu dönemde bulgu durumunda bir değişiklik olmadı."
    text = ", ".join(parts) + "."
    if work.get("average_resolution_days") is not None:
        text += f" Ortalama çözüm süresi {work['average_resolution_days']} gün."
    return text[0].upper() + text[1:]
