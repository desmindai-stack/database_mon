"""Bakım penceresi çözümü (Faz 28 İŞ 3).

Bir kesintinin ya da bir anın hangi bakım pencerelerinin kapsamına girdiğini bulur.
Kapsam hiyerarşisi bulgu kararlarıyla aynı: `instance` en dar, `global` en geniş — ve bir
instance için ÜST kapsamlardaki pencereler de geçerli. Müşteri seviyesinde tanımlanmış bir
bakım penceresi, o müşterinin her veritabanını kapsamalı; aksi halde her düğüm için ayrı
pencere açmak gerekirdi ve biri unutulduğunda o düğümün kesintisi "plansız" görünürdü.

Tek gerçeklik kaynağı: rapor bölümü, alarm motoru ve SLA hesabı bu modülden besleniyor.
Ayrı hesaplama, "raporda planlı görünen kesinti için alarm gelmesi" gibi tutarsızlıklar
üretirdi.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.maintenance import (
    Occurrence,
    classify_outage,
    expand_occurrences,
)
from app.models import Application, DatabaseGroup, Instance, MaintenanceWindow

logger = logging.getLogger(__name__)


async def _scope_chain(session: AsyncSession, instance: Instance) -> list[tuple[str, int | None]]:
    """Bir instance'ı kapsayan tüm kapsam anahtarları, dardan genişe."""
    chain: list[tuple[str, int | None]] = [("instance", instance.id)]
    group = (
        await session.get(DatabaseGroup, instance.group_id) if instance.group_id else None
    )
    if group is not None:
        chain.append(("group", group.id))
        application = await session.get(Application, group.application_id)
        if application is not None:
            chain.append(("application", application.id))
            chain.append(("customer", application.customer_id))
    chain.append(("global", None))
    return chain


async def windows_for_instance(
    session: AsyncSession, instance: Instance
) -> list[MaintenanceWindow]:
    """Bu instance'ı kapsayan etkin bakım pencereleri."""
    chain = await _scope_chain(session, instance)
    rows = (
        await session.execute(
            select(MaintenanceWindow).where(MaintenanceWindow.enabled.is_(True))
        )
    ).scalars().all()
    keys = set(chain)
    return [w for w in rows if (w.scope_type, w.scope_id) in keys]


def occurrences_in_period(
    windows: list[MaintenanceWindow], period_start: datetime, period_end: datetime
) -> list[Occurrence]:
    """Pencere kurallarını dönem içinde somut örneklere açar."""
    occurrences: list[Occurrence] = []
    for window in windows:
        # `recurrence_until` dolu ve dönem başlangıcından önceyse kural artık geçerli değil.
        limit = window.recurrence_until
        end = period_end
        if limit is not None:
            limit = limit if limit.tzinfo else limit.replace(tzinfo=UTC)
            if limit < end:
                end = limit
            if end < period_start:
                continue
        occurrences.extend(
            expand_occurrences(
                window.starts_at, window.ends_at, window.recurrence, period_start, end
            )
        )
    return occurrences


async def occurrences_for_instance(
    session: AsyncSession, instance: Instance, period_start: datetime, period_end: datetime
) -> list[Occurrence]:
    windows = await windows_for_instance(session, instance)
    return occurrences_in_period(windows, period_start, period_end)


def annotate_outages(outages: list[dict], occurrences: list[Occurrence]) -> list[dict]:
    """Kesinti kayıtlarına planlı/plansız kırılımını ekler.

    Kesintiyi bütün olarak damgalamak yerine ÖRTÜŞME ölçülüyor: bakım 02:00-04:00 iken
    03:30'da başlayıp 06:00'a kadar süren bir kesinti yarı planlı yarı plansızdır. Hepsini
    planlı saymak arızayı gizler, hepsini plansız saymak onaylanmış bakımı ceza olarak yazar.
    """
    annotated: list[dict] = []
    for outage in outages:
        start = _parse(outage.get("start"))
        end = _parse(outage.get("end"))
        if start is None or end is None:
            annotated.append({**outage, "kind": "unplanned", "planned_seconds": 0.0})
            continue
        kind, planned, unplanned = classify_outage(start, end, occurrences)
        annotated.append(
            {
                **outage,
                "kind": kind,
                "planned_seconds": round(planned, 1),
                "unplanned_seconds": round(unplanned, 1),
            }
        )
    return annotated


async def is_in_maintenance(
    session: AsyncSession, instance: Instance, moment: datetime | None = None
) -> MaintenanceWindow | None:
    """Şu an (ya da verilen an) bakım penceresinde miyiz? Pencereyi döndürür.

    Alarm motoru bunu kullanıyor: bakım sırasında alarm ÜRETİLMİYOR. Alternatif, alarmı
    üretip "bakımdaydı" diye işaretlemek olurdu; e-posta yine giderdi ve bakım gecelerinde
    nöbetçiyi uyandırmaya devam ederdi — istenen tam olarak bunun önlenmesi.
    """
    moment = moment or datetime.now(UTC)
    windows = await windows_for_instance(session, instance)
    for window in windows:
        limit = window.recurrence_until
        if limit is not None:
            limit = limit if limit.tzinfo else limit.replace(tzinfo=UTC)
            if moment > limit:
                continue
        for occurrence in expand_occurrences(
            window.starts_at, window.ends_at, window.recurrence, moment, moment
        ):
            if occurrence.covers(moment):
                return window
    return None


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
