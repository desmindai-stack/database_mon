"""Sağlık raporunun bölümleri (Faz 17).

Her bölüm `ReportContext`'ten okur ve bir `SectionResult` döndürür. Bölümler yalnızca
SAKLANMIŞ veriye bakar — canlı bağlantı açmazlar (bkz. services/health_report.py).

Kalite kuralları (Faz 17 İŞ 6) burada uygulanır:

* Her bulgu `evidence` taşır: hangi metrik, hangi değer, hangi eşik, ne zaman ölçüldü.
* Veri yetersizse bölüm `status="unknown"` + `unknown_reason` döner; "sorunsuz" demez.
* Her kritik/uyarı bulgusunun bir önerisi vardır; öneri verilemiyorsa nedeni yazılır.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Instance, MetricSample
from app.services.collection import effective_collect_interval
from app.services.health_report import (
    FindingDraft,
    ReportContext,
    SectionResult,
    register_section,
)

# Toplama aralığının kaç katı boşluk "kesinti" sayılır. 1 kaçırılan döngü ağ gecikmesi veya
# yavaş bir sorgu yüzünden olabilir; 3 katı artık gerçek bir kopukluktur.
OUTAGE_GAP_MULTIPLIER = 3
# Bu süreden kısa boşluklar raporlanmaz — 15 sn'lik toplamada 45 sn'lik bir gecikme kesinti
# değil gürültüdür.
MIN_OUTAGE_SECONDS = 60.0


def as_utc(value: datetime) -> datetime:
    """SQLite naive datetime döndürür (TIMESTAMP tipi saat dilimi taşımaz); Postgres aware
    döndürür. Rapor bölümleri iki motorda da aynı karşılaştırmayı yapabilsin diye tek noktada
    UTC'ye sabitleniyor — aksi halde dev ortamında "can't compare offset-naive and
    offset-aware datetimes" ile patlar."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _environment_of(instance: Instance) -> str:
    """Öncelik sıralaması için ortam etiketi. Grup ortamı varsa o, yoksa instance'ınki."""
    group = getattr(instance, "group", None)
    if group is not None and getattr(group, "environment", None):
        return str(group.environment)
    return str(instance.environment or "prod")


def _fmt_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} sn"
    if seconds < 5400:
        return f"{seconds / 60:.0f} dk"
    return f"{seconds / 3600:.1f} sa"


async def _outages_for_instance(ctx: ReportContext, instance: Instance) -> list[dict]:
    """Toplama boşluklarından türetilen kesinti pencereleri.

    ÖNEMLİ sınır: bu ölçüm "dbace bu instance'tan veri toplayamadı" demektir — veritabanının
    gerçekten kapalı olduğunu KANITLAMAZ (dbace worker'ı durmuş, ağ kopmuş ya da kimlik
    bilgisi geçersiz olmuş da olabilir). Rapor bunu olduğu gibi söyler; "veritabanı X dakika
    kapalıydı" diye kesin bir iddiada bulunmaz.
    """
    rows = (
        await ctx.session.execute(
            select(MetricSample.collected_at)
            .where(
                MetricSample.instance_id == instance.id,
                MetricSample.collected_at >= ctx.period_start,
                MetricSample.collected_at <= ctx.period_end,
            )
            .order_by(MetricSample.collected_at.asc())
        )
    ).scalars().all()
    if not rows:
        return []

    rows = [as_utc(r) for r in rows]
    interval = effective_collect_interval(instance)
    threshold = max(interval * OUTAGE_GAP_MULTIPLIER, MIN_OUTAGE_SECONDS)
    outages: list[dict] = []
    for previous, current in zip(rows, rows[1:]):
        gap = (current - previous).total_seconds()
        if gap >= threshold:
            outages.append(
                {
                    "start": previous.isoformat(),
                    "end": current.isoformat(),
                    "seconds": round(gap, 1),
                }
            )
    return outages


@register_section
async def availability_section(ctx: ReportContext) -> SectionResult:
    """Erişilebilirlik — kesinti sayısı ve süresi, hangi düğüm, hangi saat."""
    if not ctx.instances:
        return SectionResult(
            key="availability",
            title="Erişilebilirlik",
            status="unknown",
            summary="Bu kapsamda izlenen veritabanı yok.",
            unknown_reason="Kapsama bağlı etkin instance bulunamadı.",
        )

    period_seconds = max((ctx.period_end - ctx.period_start).total_seconds(), 1.0)
    per_instance: list[dict] = []
    findings: list[FindingDraft] = []
    no_data: list[str] = []

    for instance in ctx.instances:
        outages = await _outages_for_instance(ctx, instance)
        first_sample = (
            await ctx.session.execute(
                select(MetricSample.collected_at)
                .where(MetricSample.instance_id == instance.id)
                .order_by(MetricSample.collected_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if first_sample is None:
            no_data.append(instance.name)
            per_instance.append(
                {
                    "instance_id": instance.id,
                    "instance": instance.name,
                    "uptime_pct": None,
                    "outage_count": 0,
                    "outage_seconds": 0.0,
                    "longest_outage_seconds": 0.0,
                    "unknown_reason": "Bu instance için hiç metrik toplanmamış.",
                }
            )
            continue

        total_down = sum(o["seconds"] for o in outages)
        # Dönemin tamamı için değil, İZLENEBİLDİĞİ süre için yüzde hesaplanıyor: instance
        # dönemin ortasında eklendiyse ondan önceki zamanı "kesinti" saymak yanlış olurdu.
        observed_seconds = max(
            (ctx.period_end - max(as_utc(first_sample), ctx.period_start)).total_seconds(), 1.0
        )
        uptime_pct = max(0.0, min(100.0, (1 - total_down / observed_seconds) * 100))
        longest = max((o["seconds"] for o in outages), default=0.0)

        per_instance.append(
            {
                "instance_id": instance.id,
                "instance": instance.name,
                "uptime_pct": round(uptime_pct, 3),
                "outage_count": len(outages),
                "outage_seconds": round(total_down, 1),
                "longest_outage_seconds": round(longest, 1),
                "observed_seconds": round(observed_seconds, 1),
                "outages": outages[:20],
                "environment": _environment_of(instance),
            }
        )

        if outages:
            worst = max(outages, key=lambda o: o["seconds"])
            severity = "critical" if longest >= 300 or len(outages) >= 5 else "warning"
            findings.append(
                FindingDraft(
                    section="availability",
                    severity=severity,
                    title=f"{instance.name}: veri toplanamayan {len(outages)} dönem",
                    detail=(
                        f"Toplam {_fmt_duration(total_down)} boyunca metrik toplanamadı "
                        f"(en uzunu {_fmt_duration(longest)}, {worst['start'][11:16]} civarı). "
                        "Bu ölçüm 'dbace veri toplayamadı' demektir; veritabanının kapalı olduğunu "
                        "tek başına kanıtlamaz (worker duruşu, ağ kopması veya kimlik bilgisi "
                        "sorunu da aynı boşluğu yaratır)."
                    ),
                    evidence={
                        "metric": "metric_sample_gap",
                        "outage_count": len(outages),
                        "total_seconds": round(total_down, 1),
                        "longest_seconds": round(longest, 1),
                        "gap_threshold_seconds": max(
                            effective_collect_interval(instance) * OUTAGE_GAP_MULTIPLIER, MIN_OUTAGE_SECONDS
                        ),
                        "uptime_pct": round(uptime_pct, 3),
                        "measured_at": ctx.period_end.isoformat(),
                        "window": [ctx.period_start.isoformat(), ctx.period_end.isoformat()],
                    },
                    fingerprint_parts=("collection_gap", str(instance.id)),
                    recommendation=(
                        "Kesinti saatlerinde sunucu/servis loglarına ve dbace worker loglarına bakın; "
                        "kesinti tekrarlıyorsa Cluster sekmesinden servis durumlarını doğrulayın."
                    ),
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        if instance.enabled and first_sample is None:
            findings.append(
                FindingDraft(
                    section="availability",
                    severity="warning",
                    title=f"{instance.name}: hiç metrik toplanmamış",
                    detail="Instance etkin ama bu dönemde tek bir ölçüm bile kaydedilmemiş.",
                    evidence={
                        "metric": "metric_sample_count",
                        "value": 0,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("no_samples", str(instance.id)),
                    recommendation="Bağlantı ayarlarını 'Bağlantı testi' ile doğrulayın; worker çalışıyor mu kontrol edin.",
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

    measured = [p for p in per_instance if p["uptime_pct"] is not None]
    overall_uptime = round(sum(p["uptime_pct"] for p in measured) / len(measured), 3) if measured else None
    total_outages = sum(p["outage_count"] for p in per_instance)

    if not measured:
        status = "unknown"
        summary = "Hiçbir instance için ölçüm yok — erişilebilirlik hesaplanamadı."
    elif total_outages == 0:
        status = "ok"
        summary = f"{len(measured)} veritabanının tamamından kesintisiz veri toplandı."
    else:
        worst_uptime = min(p["uptime_pct"] for p in measured)
        status = "critical" if worst_uptime < 99.0 else "warning"
        summary = (
            f"{total_outages} kesinti dönemi tespit edildi; ortalama erişilebilirlik %{overall_uptime}."
        )

    return SectionResult(
        key="availability",
        title="Erişilebilirlik",
        status=status,
        summary=summary,
        findings=findings,
        data={
            "overall_uptime_pct": overall_uptime,
            "total_outages": total_outages,
            "instances": per_instance,
            "no_data_instances": no_data,
            "method": (
                "Erişilebilirlik, toplanan metrik örnekleri arasındaki boşluklardan türetilir "
                f"(toplama aralığının {OUTAGE_GAP_MULTIPLIER} katından uzun boşluk = kesinti)."
            ),
        },
        unknown_reason=None if measured else "Dönem içinde hiç metrik örneği yok.",
    )


def _period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(UTC)
    return end - timedelta(days=1), end
