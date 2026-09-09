"""SLA takibi (Faz 28 İŞ 3b).

Hedef olmadan erişilebilirlik sayısı bir bilgi ama bir KARAR değil: %99.7 iyi mi kötü mü,
ancak taahhüde göre söylenebilir.

## İki tahmin metriği neden çıplak yüzdeden değerli

Ayın 3'ünde "%99.2" görmek yöneticiye hiçbir şey söylemiyor: ay dolmadı, sayı daha
değişecek. İki türev sayı asıl kararı veriyor:

* **En iyi durum** — kalan dönem kesintisiz geçerse ulaşılabilecek oran. Bu sayı hedefin
  altındaysa **ay matematiksel olarak kaybedilmiştir** ve bunu ayın 3'ünde bilmek, ayın
  30'unda öğrenmekten çok farklı bir yönetim kararı üretir.
* **Kalan kesinti bütçesi** — SLA'yı ihlal etmeden karşılanabilecek azami kesinti süresi.
  "47 dakikanız kaldı" cümlesi, "%99.2" cümlesinden kıyas kabul etmeyecek kadar
  eyleme dönük: bakım planlamak için doğrudan kullanılabilir.

## Çok veritabanlı kapsamda toplama kararı

Bir uygulamanın üç veritabanı varsa "uygulamanın erişilebilirliği" tek bir doğru cevabı
olmayan bir soru: replikası olan bir kümede bir düğümün düşmesi uygulama için kesinti
DEĞİL, ama dbace replikanın devraldığını bilmiyor.

Seçilen tanım: **kapsamdaki veritabanlarının ortalaması** — çünkü erişilebilirlik bölümü
zaten bu tanımı kullanıyor ve iki yerin farklı sayı göstermesi güven kaybı olurdu. "Kalan
bütçe" de aynı ortalamadan türetiliyor; ayrıca **en kötü veritabanı** ayrı gösteriliyor ki
ortalamanın gizlediği düğüm görünsün.

Sınır SORULAR.md'de açıkça yazılı: bu ölçüm **veritabanı erişilebilirliği**, uygulama
erişilebilirliği değil.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Instance, SlaTarget
from app.services.availability import first_sample_at, outages_for_instance
from app.services.maintenance import annotate_outages, occurrences_for_instance

logger = logging.getLogger(__name__)

PERIOD_MONTHLY = "monthly"
PERIOD_QUARTERLY = "quarterly"

PERIOD_LABELS: dict[str, str] = {
    PERIOD_MONTHLY: "Aylık",
    PERIOD_QUARTERLY: "Çeyreklik",
}


def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Ölçüm döneminin başlangıcı ve bitişi (dönemin TAMAMI, bugüne kadarki kısmı değil).

    Bitişin dönemin sonu olması şart: "kalan bütçe" ve "en iyi durum" hesapları kalan süreyi
    bilmek zorunda. Bugüne kadarki kısım ayrıca `elapsed` olarak veriliyor.
    """
    now = now or datetime.now(UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    if period == PERIOD_QUARTERLY:
        quarter_start_month = 3 * ((now.month - 1) // 3) + 1
        start = now.replace(
            month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        end_month = quarter_start_month + 3
        end = (
            start.replace(year=start.year + 1, month=end_month - 12)
            if end_month > 12
            else start.replace(month=end_month)
        )
        return start, end
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


@dataclass
class InstanceAvailability:
    instance_id: int
    instance_name: str
    #: İzlenebildiği süre (instance dönem ortasında eklendiyse dönemden kısa).
    observed_seconds: float
    planned_seconds: float
    unplanned_seconds: float
    uptime_pct: float | None


@dataclass
class SlaStatus:
    """Bir hedefin dönem içindeki durumu."""

    scope_type: str
    scope_id: int | None
    scope_label: str
    target_pct: float
    period: str
    period_start: datetime
    period_end: datetime
    elapsed_seconds: float
    remaining_seconds: float

    measured: bool = False
    #: Dönemin BUGÜNE KADARKİ kısmında gerçekleşen oran.
    achieved_pct: float | None = None
    planned_seconds: float = 0.0
    unplanned_seconds: float = 0.0
    #: Kalan dönem kesintisiz geçerse ulaşılabilecek oran.
    best_case_pct: float | None = None
    #: SLA'yı ihlal etmeden kalan kesinti süresi. Negatif = hedef zaten aşıldı.
    remaining_budget_seconds: float | None = None
    met: bool | None = None
    #: Hedef, kalan süre kesintisiz geçse bile tutturulamıyor mu.
    already_lost: bool = False
    instances: list[InstanceAvailability] = field(default_factory=list)
    worst_instance: str | None = None
    unknown_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "scope_label": self.scope_label,
            "target_pct": self.target_pct,
            "period": self.period,
            "period_label": PERIOD_LABELS.get(self.period, self.period),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "remaining_seconds": round(self.remaining_seconds, 1),
            "measured": self.measured,
            "achieved_pct": self.achieved_pct,
            "planned_seconds": round(self.planned_seconds, 1),
            "unplanned_seconds": round(self.unplanned_seconds, 1),
            "best_case_pct": self.best_case_pct,
            "remaining_budget_seconds": (
                round(self.remaining_budget_seconds, 1)
                if self.remaining_budget_seconds is not None
                else None
            ),
            "met": self.met,
            "already_lost": self.already_lost,
            "worst_instance": self.worst_instance,
            "unknown_reason": self.unknown_reason,
            "instances": [
                {
                    "instance_id": row.instance_id,
                    "instance": row.instance_name,
                    "uptime_pct": row.uptime_pct,
                    "planned_seconds": round(row.planned_seconds, 1),
                    "unplanned_seconds": round(row.unplanned_seconds, 1),
                }
                for row in self.instances
            ],
        }


async def _instance_availability(
    session: AsyncSession, instance: Instance, period_start: datetime, measure_end: datetime
) -> InstanceAvailability:
    outages = await outages_for_instance(session, instance, period_start, measure_end)
    occurrences = await occurrences_for_instance(session, instance, period_start, measure_end)
    outages = annotate_outages(outages, occurrences)

    planned = sum(o.get("planned_seconds", 0.0) for o in outages)
    unplanned = sum(o.get("unplanned_seconds", 0.0) for o in outages)

    first = await first_sample_at(session, instance)
    if first is None:
        # Hiç ölçüm yok: "%100 ayakta" DEMEK DEĞİL. Ölçülemeyen bir instance ortalamayı
        # yukarı çekmemeli, yoksa izlenmeyen bir sunucu SLA'yı kurtarır hale gelirdi.
        return InstanceAvailability(instance.id, instance.name, 0.0, 0.0, 0.0, None)

    observed = max((measure_end - max(first, period_start)).total_seconds(), 1.0)
    uptime = max(0.0, min(100.0, (1 - unplanned / observed) * 100))
    return InstanceAvailability(instance.id, instance.name, observed, planned, unplanned, uptime)


async def evaluate_target(
    session: AsyncSession, target: SlaTarget, now: datetime | None = None
) -> SlaStatus:
    """Bir SLA hedefinin güncel durumu."""
    from app.services.health_report import ReportScope, resolve_scope_instances, resolve_scope_label

    now = now or datetime.now(UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    period_start, period_end = period_bounds(target.period, now)
    # Ölçüm bugüne kadar; hedef kontrolü dönemin TAMAMI üzerinden.
    measure_end = min(now, period_end)
    elapsed = max((measure_end - period_start).total_seconds(), 1.0)
    remaining = max((period_end - measure_end).total_seconds(), 0.0)
    period_total = max((period_end - period_start).total_seconds(), 1.0)

    label = await resolve_scope_label(session, target.scope_type, target.scope_id)
    status = SlaStatus(
        scope_type=target.scope_type,
        scope_id=target.scope_id,
        scope_label=label,
        target_pct=float(target.target_pct),
        period=target.period,
        period_start=period_start,
        period_end=period_end,
        elapsed_seconds=elapsed,
        remaining_seconds=remaining,
    )

    instances = await resolve_scope_instances(
        session, ReportScope(target.scope_type, target.scope_id, label)
    )
    if not instances:
        status.unknown_reason = "Bu kapsamda izlenen veritabanı yok."
        return status

    rows = [
        await _instance_availability(session, instance, period_start, measure_end)
        for instance in instances
    ]
    status.instances = rows
    measured = [r for r in rows if r.uptime_pct is not None]
    if not measured:
        status.unknown_reason = (
            "Bu dönemde hiç ölçüm alınmamış — erişilebilirlik hesaplanamıyor. Bu, sistemin "
            "ayakta olduğu anlamına GELMEZ."
        )
        return status

    status.measured = True
    status.planned_seconds = sum(r.planned_seconds for r in measured)
    status.unplanned_seconds = sum(r.unplanned_seconds for r in measured)
    status.achieved_pct = round(sum(r.uptime_pct for r in measured) / len(measured), 4)
    worst = min(measured, key=lambda r: r.uptime_pct)
    status.worst_instance = worst.instance_name

    # EŞDEĞER KESİNTİ: ortalama orandan türetiliyor ki "kalan bütçe" ile "gerçekleşen oran"
    # aynı tanımdan gelsin. Biri ortalamaya, diğeri en kötü düğüme dayansaydı iki sayı
    # birbiriyle çelişir ve hangisine güvenileceği belirsiz kalırdı.
    equivalent_downtime = (1 - status.achieved_pct / 100) * elapsed

    # EN İYİ DURUM: kalan süre kesintisiz geçerse dönem sonunda ulaşılabilecek oran.
    status.best_case_pct = round(
        max(0.0, min(100.0, (1 - equivalent_downtime / period_total) * 100)), 4
    )
    allowed = period_total * (1 - status.target_pct / 100)
    status.remaining_budget_seconds = allowed - equivalent_downtime

    status.met = status.achieved_pct >= status.target_pct
    # Hedef matematiksel olarak kaybedilmiş mi: kalan süre KUSURSUZ geçse bile tutmuyorsa.
    status.already_lost = status.best_case_pct < status.target_pct
    return status


async def evaluate_all(session: AsyncSession, now: datetime | None = None) -> list[SlaStatus]:
    targets = (
        await session.execute(select(SlaTarget).where(SlaTarget.enabled.is_(True)))
    ).scalars().all()
    results: list[SlaStatus] = []
    for target in targets:
        try:
            results.append(await evaluate_target(session, target, now))
        except Exception:
            # Tek bir hedefin çökmesi diğerlerini gizlememeli; sessizce atlamak da yanlış
            # olurdu, o yüzden loglanıyor.
            logger.exception(
                "SLA hedefi değerlendirilemedi (scope=%s/%s)", target.scope_type, target.scope_id
            )
    return results


async def targets_for_instances(
    session: AsyncSession, instances: list[Instance]
) -> list[SlaTarget]:
    """Verilen instance'ları kapsayan hedefler — rapor bölümü için.

    Rapor kapsamı bir müşteri olabilir ama hedef uygulama seviyesinde tanımlanmış olabilir;
    hedefi yalnızca tam eşleşen kapsamda aramak, tanımlı bir SLA'nın raporda hiç
    görünmemesine yol açardı.
    """
    from app.services.finding_status import build_scope_membership

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

    rows = (
        await session.execute(select(SlaTarget).where(SlaTarget.enabled.is_(True)))
    ).scalars().all()
    return [t for t in rows if (t.scope_type, t.scope_id) in keys]


def format_duration(seconds: float) -> str:
    """Kesinti bütçesi için okunur süre. Negatif değer "aşıldı" demek ve gizlenmiyor."""
    negative = seconds < 0
    value = abs(seconds)
    if value < 90:
        text = f"{value:.0f} saniye"
    elif value < 5400:
        text = f"{value / 60:.0f} dakika"
    elif value < 172800:
        text = f"{value / 3600:.1f} saat"
    else:
        text = f"{value / 86400:.1f} gün"
    return f"-{text}" if negative else text
