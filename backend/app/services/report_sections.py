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

from app.models import (
    AlertEvent,
    AlertRule,
    DailyStateSnapshot,
    FindingAcknowledgement,
    GroupHealthSnapshot,
    Instance,
    MetricSample,
    PredictionInsight,
    SchemaObjectDailySample,
    SlowQuerySample,
)
from app.services.collection import effective_collect_interval
from app.services.health_report import (
    FindingDraft,
    ReportContext,
    SectionResult,
    make_fingerprint,
    register_section,
    register_summary_section,
)
from app.services.query_diagnostics import diagnose_query

# Toplama aralığının kaç katı boşluk "kesinti" sayılır. 1 kaçırılan döngü ağ gecikmesi veya
# yavaş bir sorgu yüzünden olabilir; 3 katı artık gerçek bir kopukluktur.
OUTAGE_GAP_MULTIPLIER = 3
# Bu süreden kısa boşluklar raporlanmaz — 15 sn'lik toplamada 45 sn'lik bir gecikme kesinti
# değil gürültüdür.
MIN_OUTAGE_SECONDS = 60.0


# --- Eşikler ---------------------------------------------------------------------------
# Bu eşikler raporun "neyi bulgu sayacağını" belirler; hepsi tek yerde ve gerekçeli.

# Bağlantı doluluğu: %85 mevcut alarm/insight eşikleriyle aynı (performance_insights.py),
# iki yerin farklı eşik kullanması tutarsızlık yaratırdı.
CONNECTION_UTIL_WARN = 85.0
CONNECTION_UTIL_CRITICAL = 95.0
# Cache hit: %90 altı PostgreSQL için genel kabul gören "diske çok gidiyor" sınırı.
CACHE_HIT_WARN = 90.0
# Bir sorgunun rapora bulgu olarak girmesi için gereken en düşük ortalama süre.
SLOW_QUERY_MEAN_MS = 50.0
TOP_QUERIES = 10
# Bir alarm kuralının "gürültü yapıyor" sayılması için dönemdeki tetikleme sayısı.
NOISY_RULE_THRESHOLD = 10

_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1, "ok": 0}
_ENV_RANK = {"prod": 1.6, "production": 1.6, "preprod": 1.1, "test": 0.9, "dev": 0.8}


def _worst_status(severities: list[str]) -> str:
    if not severities:
        return "ok"
    worst = max(_SEVERITY_RANK.get(s, 0) for s in severities)
    return {3: "critical", 2: "warning", 1: "info", 0: "ok"}[worst]


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row.get(key))] = out.get(str(row.get(key)), 0) + 1
    return out


def _short_query(query: str, limit: int = 120) -> str:
    collapsed = " ".join(query.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def _format_bytes(value: float) -> str:
    step = 1024.0
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < step:
            return f"{amount:.1f} {unit}"
        amount /= step
    return f"{amount:.1f} PB"


async def _samples_in_period(ctx: ReportContext, instance: Instance) -> list[MetricSample]:
    """Dönem içindeki metrik örnekleri, zamana göre sıralı. Bölümler bunu paylaşır."""
    rows = (
        await ctx.session.execute(
            select(MetricSample)
            .where(
                MetricSample.instance_id == instance.id,
                MetricSample.collected_at >= ctx.period_start,
                MetricSample.collected_at <= ctx.period_end,
            )
            .order_by(MetricSample.collected_at.asc())
        )
    ).scalars().all()
    return list(rows)


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


# --------------------------------------------------------------------------------------
# 4. Cluster sağlığı
# --------------------------------------------------------------------------------------


def _cluster_snapshots(samples: list[MetricSample]) -> list[tuple[datetime, dict]]:
    """Örneklerin içine gömülü cluster anlık görüntüleri (collection.py bunları
    metrics_json["cluster_services"] altına yazar) — lider değişimi ve servis kesintileri
    buradan, geriye dönük olarak okunur."""
    out: list[tuple[datetime, dict]] = []
    for sample in samples:
        snapshot = (sample.metrics_json or {}).get("cluster_services")
        if isinstance(snapshot, dict):
            out.append((as_utc(sample.collected_at), snapshot))
    return out


@register_section
async def cluster_section(ctx: ReportContext) -> SectionResult:
    """Cluster sağlığı — lider değişimi, replikasyon lag zirvesi, quorum, split-brain, DR."""
    clustered = [i for i in ctx.instances if i.cluster_name or (i.services or [])]
    if not clustered:
        return SectionResult(
            key="cluster",
            title="Cluster sağlığı",
            status="ok",
            summary="Bu kapsamda cluster yapılandırılmış düğüm yok (standalone kurulum).",
            data={"clustered_instances": 0},
        )

    findings: list[FindingDraft] = []
    per_instance: list[dict] = []
    leader_changes_total = 0

    for instance in clustered:
        samples = await _samples_in_period(ctx, instance)
        snapshots = _cluster_snapshots(samples)

        # Lider değişimi: ardışık anlık görüntülerde cluster.leader'ın değişmesi.
        leaders = [(at, (snap.get("cluster") or {}).get("leader")) for at, snap in snapshots]
        leaders = [(at, name) for at, name in leaders if name]
        changes = [
            {"at": at.isoformat(), "from": previous, "to": current}
            for (_, previous), (at, current) in zip(leaders, leaders[1:])
            if previous != current
        ]
        leader_changes_total += len(changes)

        # Replikasyon lag zirvesi ve zirvenin üstünde geçirilen süre.
        lag_values = [(as_utc(s.collected_at), float(s.replication_lag_bytes or 0)) for s in samples]
        lag_values = [(at, v) for at, v in lag_values if v > 0]
        peak_lag = max((v for _, v in lag_values), default=0.0)
        peak_at = next((at for at, v in lag_values if v == peak_lag), None)
        # "Yüksek lag" süresi: zirvenin yarısını aşan örneklerin sayısı × toplama aralığı.
        high_lag_samples = [v for _, v in lag_values if peak_lag > 0 and v >= peak_lag / 2]
        high_lag_seconds = len(high_lag_samples) * effective_collect_interval(instance)

        down_services: dict[str, int] = {}
        no_leader_samples = 0
        for _at, snap in snapshots:
            for svc in snap.get("services") or []:
                if svc.get("status") == "down":
                    down_services[str(svc.get("service"))] = down_services.get(str(svc.get("service")), 0) + 1
            cluster = snap.get("cluster") or {}
            if cluster and cluster.get("has_leader") is False:
                no_leader_samples += 1

        per_instance.append(
            {
                "instance_id": instance.id,
                "instance": instance.name,
                "cluster_name": instance.cluster_name,
                "role": instance.role,
                "leader_changes": len(changes),
                "leader_change_events": changes[:10],
                "peak_replication_lag_bytes": peak_lag,
                "peak_at": peak_at.isoformat() if peak_at else None,
                "high_lag_seconds": round(high_lag_seconds, 1),
                "down_service_samples": down_services,
                "no_leader_samples": no_leader_samples,
                "snapshot_count": len(snapshots),
            }
        )

        if changes:
            findings.append(
                FindingDraft(
                    section="cluster",
                    severity="warning" if len(changes) == 1 else "critical",
                    title=f"{instance.name}: {len(changes)} lider değişimi",
                    detail=(
                        "Dönem içinde cluster lideri değişti: "
                        + "; ".join(f"{c['from']} → {c['to']} ({c['at'][11:16]})" for c in changes[:3])
                        + ". Planlı bir failover değilse sebebi araştırılmalı."
                    ),
                    evidence={
                        "metric": "cluster.leader",
                        "value": len(changes),
                        "events": changes[:10],
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("leader_change", str(instance.id)),
                    recommendation=(
                        "Patroni ve PostgreSQL loglarını değişim saatlerinde inceleyin; "
                        "etcd erişilebilirliğini ve düğüm kaynak kullanımını kontrol edin."
                    ),
                    commands=["patronictl -c /etc/patroni.yml history", "patronictl -c /etc/patroni.yml list"],
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        if no_leader_samples:
            findings.append(
                FindingDraft(
                    section="cluster",
                    severity="critical",
                    title=f"{instance.name}: lidersiz kalınan dönem",
                    detail=(
                        f"{no_leader_samples} ölçümde cluster'ın lideri yoktu — bu süre boyunca "
                        "yazma işlemleri kabul edilmemiş olabilir."
                    ),
                    evidence={
                        "metric": "cluster.has_leader",
                        "value": 0,
                        "sample_count": no_leader_samples,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("no_leader", str(instance.id)),
                    recommendation="Patroni/etcd durumunu ve quorum'u doğrulayın.",
                    commands=["patronictl -c /etc/patroni.yml list", "etcdctl endpoint health --cluster"],
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        for service, count in down_services.items():
            findings.append(
                FindingDraft(
                    section="cluster",
                    severity="critical" if service in ("postgresql", "patroni", "etcd") else "warning",
                    title=f"{instance.name}: {service} servisi {count} ölçümde kapalıydı",
                    detail=f"Servis dönem içinde {count} ölçümde 'down' raporlandı.",
                    evidence={
                        "metric": f"cluster_service.{service}",
                        "value": count,
                        "sample_count": len(snapshots),
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("service_down", str(instance.id), service),
                    recommendation=f"Sunucuda `systemctl status {service}` ile durumu ve loglarını inceleyin.",
                    commands=[f"systemctl status {service}", f"journalctl -u {service} --since '1 day ago'"],
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

    # Grup seviyesi: quorum, split-brain, DR kapsamı — GroupHealthSnapshot'tan (canlı probe yok).
    group_ids = sorted({i.group_id for i in clustered if i.group_id})
    group_rows: list[dict] = []
    if group_ids:
        snapshots = (
            await ctx.session.execute(
                select(GroupHealthSnapshot).where(GroupHealthSnapshot.group_id.in_(group_ids))
            )
        ).scalars().all()
        for snapshot in snapshots:
            report = snapshot.report_json or {}
            quorum = report.get("etcd_quorum") or {}
            dr_nodes = [n for n in (report.get("nodes") or []) if n.get("site") == "disaster"]
            group_rows.append(
                {
                    "group_id": snapshot.group_id,
                    "overall": snapshot.overall,
                    "checked_at": snapshot.checked_at.isoformat() if snapshot.checked_at else None,
                    "has_quorum": quorum.get("has_quorum"),
                    "etcd_up": quorum.get("up"),
                    "etcd_total": quorum.get("total"),
                    "split_brain": bool(report.get("split_brain")),
                    "down_nodes": report.get("down_nodes") or [],
                    "dr_node_count": len(dr_nodes),
                }
            )

            if report.get("split_brain"):
                findings.append(
                    FindingDraft(
                        section="cluster",
                        severity="critical",
                        title=f"Grup #{snapshot.group_id}: split-brain şüphesi",
                        detail=(
                            "Birden fazla düğüm VIP sahibi görünüyor: "
                            f"{', '.join(report.get('split_brain_nodes') or [])}. "
                            "Aynı anda iki yazılabilir düğüm veri kaybına yol açabilir."
                        ),
                        evidence={
                            "metric": "group.split_brain",
                            "value": True,
                            "nodes": report.get("split_brain_nodes") or [],
                            "measured_at": (snapshot.checked_at or ctx.period_end).isoformat()
                            if snapshot.checked_at
                            else ctx.period_end.isoformat(),
                        },
                        fingerprint_parts=("split_brain", str(snapshot.group_id)),
                        recommendation="Keepalived/VIP sahipliğini derhal doğrulayın; yanlış düğümde VIP varsa servisi durdurun.",
                        commands=["ip -4 addr show", "systemctl status keepalived"],
                        related_object_type="group",
                        related_object_id=snapshot.group_id,
                    )
                )

            if quorum.get("total") and not quorum.get("has_quorum", True):
                findings.append(
                    FindingDraft(
                        section="cluster",
                        severity="critical",
                        title=f"Grup #{snapshot.group_id}: etcd quorum kaybı",
                        detail=(
                            f"{quorum.get('up')}/{quorum.get('total')} etcd düğümü ayakta "
                            f"(gereken {quorum.get('quorum_size')}). Quorum olmadan Patroni failover yapamaz."
                        ),
                        evidence={
                            "metric": "etcd.quorum",
                            "value": quorum.get("up"),
                            "threshold": quorum.get("quorum_size"),
                            "total": quorum.get("total"),
                            "measured_at": snapshot.checked_at.isoformat()
                            if snapshot.checked_at
                            else ctx.period_end.isoformat(),
                        },
                        fingerprint_parts=("etcd_quorum", str(snapshot.group_id)),
                        recommendation="Kapalı etcd düğümlerini ayağa kaldırın; disk/ağ sorunlarını kontrol edin.",
                        commands=["etcdctl endpoint health --cluster", "systemctl status etcd"],
                        related_object_type="group",
                        related_object_id=snapshot.group_id,
                    )
                )

            if group_rows and not dr_nodes:
                findings.append(
                    FindingDraft(
                        section="cluster",
                        severity="info",
                        title=f"Grup #{snapshot.group_id}: felaket kurtarma (DR) düğümü tanımlı değil",
                        detail="Bu grupta 'disaster' sitesinde düğüm yok — site bazlı bir arıza tüm grubu etkiler.",
                        evidence={
                            "metric": "group.dr_node_count",
                            "value": 0,
                            "measured_at": ctx.period_end.isoformat(),
                        },
                        fingerprint_parts=("no_dr_node", str(snapshot.group_id)),
                        recommendation="DR gereksinimi varsa disaster sitesine bir replika düğüm ekleyin.",
                        related_object_type="group",
                        related_object_id=snapshot.group_id,
                    )
                )

    if not any(p["snapshot_count"] for p in per_instance) and not group_rows:
        return SectionResult(
            key="cluster",
            title="Cluster sağlığı",
            status="unknown",
            summary="Cluster durumu bu dönem için kaydedilmemiş.",
            data={"instances": per_instance},
            unknown_reason=(
                "Dönem içindeki metrik örneklerinde cluster anlık görüntüsü yok — cluster "
                "servisleri tanımlı olsa da sağlık toplama çalışmamış olabilir."
            ),
        )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    summary = (
        f"{len(clustered)} cluster düğümü izlendi; {leader_changes_total} lider değişimi, "
        f"{sum(1 for g in group_rows if g['split_brain'])} split-brain şüphesi."
        if findings
        else f"{len(clustered)} cluster düğümünde lider değişimi, quorum kaybı veya servis kesintisi görülmedi."
    )
    return SectionResult(
        key="cluster",
        title="Cluster sağlığı",
        status=status,
        summary=summary,
        findings=findings,
        data={"instances": per_instance, "groups": group_rows},
    )


# --------------------------------------------------------------------------------------
# 5. Performans (en pahalı sorgular)
# --------------------------------------------------------------------------------------


async def _query_window(ctx: ReportContext, instance_id: int, start: datetime, end: datetime) -> dict[str, dict]:
    """queryid → pencere içindeki DEĞİŞİM (kümülatif sayaç farkı).

    Faz 16-B İŞ 4'te yavaş sorgu listesi için kurulan mantığın aynısı: pencerede en çok süre
    harcayan sorgu, son kümülatif değeri en büyük olan sorgu DEĞİLDİR.
    """
    rows = (
        await ctx.session.execute(
            select(SlowQuerySample)
            .where(
                SlowQuerySample.instance_id == instance_id,
                SlowQuerySample.collected_at >= start,
                SlowQuerySample.collected_at <= end,
            )
            .order_by(SlowQuerySample.collected_at.asc())
        )
    ).scalars().all()

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.queryid or row.query, []).append(row)

    out: dict[str, dict] = {}
    for key, group in grouped.items():
        first, last = group[0], group[-1]
        reset = last.total_time_ms < first.total_time_ms or last.calls < first.calls
        total = last.total_time_ms if reset else last.total_time_ms - first.total_time_ms
        calls = last.calls if reset else last.calls - first.calls
        if total <= 0:
            continue
        out[key] = {
            "queryid": last.queryid,
            "query": last.query,
            "total_time_ms": round(total, 2),
            "calls": calls,
            "mean_time_ms": round(total / calls, 2) if calls > 0 else last.mean_time_ms,
            "row": last,
        }
    return out


@register_section
async def performance_section(ctx: ReportContext) -> SectionResult:
    """Performans — en pahalı 10 sorgu, düne göre değişim, darboğaz sınıfı."""
    window = ctx.period_end - ctx.period_start
    previous_start, previous_end = ctx.period_start - window, ctx.period_start

    findings: list[FindingDraft] = []
    all_rows: list[dict] = []
    instances_without_data: list[str] = []

    for instance in ctx.instances:
        current = await _query_window(ctx, instance.id, ctx.period_start, ctx.period_end)
        if not current:
            instances_without_data.append(instance.name)
            continue
        previous = await _query_window(ctx, instance.id, previous_start, previous_end)

        ranked = sorted(current.values(), key=lambda q: q["total_time_ms"], reverse=True)[:TOP_QUERIES]
        for entry in ranked:
            key = entry["queryid"] or entry["query"]
            before = previous.get(key)
            if before is None:
                change = "new"
                change_pct = None
            else:
                base = before["total_time_ms"] or 1
                change_pct = round((entry["total_time_ms"] - base) / base * 100, 1)
                change = "worse" if change_pct >= 25 else "better" if change_pct <= -25 else "stable"

            diagnosis = diagnose_query(entry["row"])
            all_rows.append(
                {
                    "instance_id": instance.id,
                    "instance": instance.name,
                    "queryid": entry["queryid"],
                    "query": entry["query"][:500],
                    "total_time_ms": entry["total_time_ms"],
                    "calls": entry["calls"],
                    "mean_time_ms": entry["mean_time_ms"],
                    "change": change,
                    "change_pct": change_pct,
                    "resource": diagnosis.resource,
                    "resource_reason": diagnosis.reason,
                    "resource_confidence": diagnosis.confidence,
                }
            )

            # Bulgu yalnızca gerçekten dikkat isteyen sorgular için: yeni ortaya çıkmış ya da
            # belirgin kötüleşmiş olanlar. "En pahalı 10" listesinin tamamını bulguya çevirmek
            # her gün 10 bulgu üretir ve gürültü olurdu.
            if change in ("new", "worse") and entry["mean_time_ms"] >= SLOW_QUERY_MEAN_MS:
                findings.append(
                    FindingDraft(
                        section="performance",
                        severity="warning" if change == "worse" else "info",
                        title=(
                            f"{instance.name}: {'kötüleşen' if change == 'worse' else 'yeni'} pahalı sorgu "
                            f"({diagnosis.resource} darboğazı)"
                        ),
                        detail=(
                            f"{_short_query(entry['query'])} — dönemde {entry['calls']} çağrı, "
                            f"toplam {entry['total_time_ms']:.0f} ms, ortalama {entry['mean_time_ms']:.1f} ms"
                            + (f" (önceki döneme göre %{change_pct:+.0f})." if change_pct is not None else " (önceki dönemde yoktu).")
                            + f" Darboğaz: {diagnosis.reason}"
                        ),
                        evidence={
                            "metric": "pg_stat_statements.total_exec_time (dönem farkı)",
                            "value": entry["total_time_ms"],
                            "mean_time_ms": entry["mean_time_ms"],
                            "calls": entry["calls"],
                            "change_pct": change_pct,
                            "resource": diagnosis.resource,
                            "resource_confidence": diagnosis.confidence,
                            "queryid": entry["queryid"],
                            "measured_at": ctx.period_end.isoformat(),
                        },
                        fingerprint_parts=("slow_query", str(instance.id), str(entry["queryid"] or "")[:32]),
                        recommendation=(
                            "Instance detayındaki Yavaş Sorgular sekmesinde bu sorgu için EXPLAIN planına ve "
                            "index önerisine bakın."
                        ),
                        related_object_type="instance",
                        related_object_id=instance.id,
                        environment=_environment_of(instance),
                    )
                )

    if not all_rows:
        return SectionResult(
            key="performance",
            title="Performans",
            status="unknown",
            summary="Bu dönem için yavaş sorgu örneği yok.",
            data={"instances_without_data": instances_without_data},
            unknown_reason=(
                "Dönem içinde hiç yavaş sorgu örneği kaydedilmemiş. pg_stat_statements ön koşulları "
                "eksik olabilir — Ön koşullar bölümüne bakın."
            ),
        )

    all_rows.sort(key=lambda r: r["total_time_ms"], reverse=True)
    worse = [r for r in all_rows if r["change"] == "worse"]
    better = [r for r in all_rows if r["change"] == "better"]
    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    return SectionResult(
        key="performance",
        title="Performans",
        status=status,
        summary=(
            f"En pahalı {len(all_rows)} sorgu incelendi: {len(worse)} kötüleşen, {len(better)} düzelen, "
            f"{sum(1 for r in all_rows if r['change'] == 'new')} yeni."
        ),
        findings=findings,
        data={
            "top_queries": all_rows[: TOP_QUERIES * 2],
            "by_resource": _count_by(all_rows, "resource"),
            "instances_without_data": instances_without_data,
        },
    )


# --------------------------------------------------------------------------------------
# 6. Kaynak kullanımı
# --------------------------------------------------------------------------------------


@register_section
async def resources_section(ctx: ReportContext) -> SectionResult:
    """Kaynak kullanımı — bağlantı zirvesi, cache hit trendi, geçici dosya, checkpoint."""
    findings: list[FindingDraft] = []
    rows: list[dict] = []

    for instance in ctx.instances:
        samples = await _samples_in_period(ctx, instance)
        if not samples:
            continue

        conn_values = [(as_utc(s.collected_at), s.active_connections or 0) for s in samples]
        peak_conn, peak_conn_at = max(((v, at) for at, v in conn_values), default=(0, None))
        max_conn = next((s.max_connections for s in reversed(samples) if s.max_connections), None)
        peak_util = round(peak_conn / max_conn * 100, 1) if max_conn else None

        cache_values = [float(s.cache_hit_ratio) for s in samples if s.cache_hit_ratio is not None]
        cache_avg = round(sum(cache_values) / len(cache_values), 2) if cache_values else None
        cache_min = round(min(cache_values), 2) if cache_values else None

        temp_bytes = [float(s.temp_bytes or 0) for s in samples]
        temp_peak = max(temp_bytes, default=0.0)

        checkpoints_req = [float(s.get_metric("checkpoints_req") or 0) for s in samples]
        checkpoints_timed = [float(s.get_metric("checkpoints_timed") or 0) for s in samples]
        # Kümülatif sayaçlar — dönemdeki artış anlamlı olan.
        req_delta = max(0.0, (checkpoints_req[-1] - checkpoints_req[0])) if checkpoints_req else 0.0
        timed_delta = max(0.0, (checkpoints_timed[-1] - checkpoints_timed[0])) if checkpoints_timed else 0.0

        rows.append(
            {
                "instance_id": instance.id,
                "instance": instance.name,
                "peak_connections": peak_conn,
                "peak_connections_at": peak_conn_at.isoformat() if peak_conn_at else None,
                "max_connections": max_conn,
                "peak_utilization_pct": peak_util,
                "cache_hit_avg": cache_avg,
                "cache_hit_min": cache_min,
                "temp_bytes_peak": temp_peak,
                "checkpoints_requested": req_delta,
                "checkpoints_timed": timed_delta,
                "sample_count": len(samples),
            }
        )

        if peak_util is not None and peak_util >= CONNECTION_UTIL_WARN:
            findings.append(
                FindingDraft(
                    section="resources",
                    severity="critical" if peak_util >= CONNECTION_UTIL_CRITICAL else "warning",
                    title=f"{instance.name}: bağlantı doluluğu zirvede %{peak_util}",
                    detail=(
                        f"En yüksek {peak_conn}/{max_conn} bağlantı "
                        f"({peak_conn_at.strftime('%H:%M') if peak_conn_at else '—'} civarı). "
                        "Doluluk %100'e ulaşırsa yeni bağlantılar reddedilir."
                    ),
                    evidence={
                        "metric": "connection_utilization_pct",
                        "value": peak_util,
                        "threshold": CONNECTION_UTIL_WARN,
                        "active_connections": peak_conn,
                        "max_connections": max_conn,
                        "measured_at": peak_conn_at.isoformat() if peak_conn_at else ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("connection_peak", str(instance.id)),
                    recommendation=(
                        "Bağlantı havuzu (PgBouncer) kullanımını ve uygulama tarafı havuz boyutunu gözden "
                        "geçirin; 'idle in transaction' oturumları temizleyin."
                    ),
                    commands=[
                        "SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;",
                    ],
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        if cache_avg is not None and cache_avg < CACHE_HIT_WARN:
            findings.append(
                FindingDraft(
                    section="resources",
                    severity="warning",
                    title=f"{instance.name}: cache hit oranı düşük (%{cache_avg})",
                    detail=(
                        f"Dönem ortalaması %{cache_avg}, en düşük %{cache_min}. Veri diskten okunuyor; "
                        "shared_buffers yetersiz ya da sorgular gereksiz çok veri tarıyor olabilir."
                    ),
                    evidence={
                        "metric": "cache_hit_ratio",
                        "value": cache_avg,
                        "threshold": CACHE_HIT_WARN,
                        "min_value": cache_min,
                        "sample_count": len(cache_values),
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("cache_hit", str(instance.id)),
                    recommendation=(
                        "Parametre denetiminde shared_buffers/effective_cache_size değerlerine bakın; "
                        "en pahalı sorguların index kullanıp kullanmadığını kontrol edin."
                    ),
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        if temp_peak > 0:
            findings.append(
                FindingDraft(
                    section="resources",
                    severity="info",
                    title=f"{instance.name}: geçici dosya kullanımı var",
                    detail=(
                        f"Zirve {_format_bytes(temp_peak)} geçici dosya. Sıralama/hash işlemleri work_mem'e "
                        "sığmayıp diske taşıyor."
                    ),
                    evidence={
                        "metric": "temp_bytes",
                        "value": temp_peak,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("temp_files", str(instance.id)),
                    recommendation="Geçici dosya üreten sorguları Performans bölümünden bulup work_mem'i sorgu bazında artırmayı değerlendirin.",
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        # İstek üzerine checkpoint (requested), zamanlanmışa göre baskınsa WAL baskısı vardır.
        if req_delta > 0 and req_delta > timed_delta:
            findings.append(
                FindingDraft(
                    section="resources",
                    severity="warning",
                    title=f"{instance.name}: checkpoint'ler zamanından önce tetikleniyor",
                    detail=(
                        f"Dönemde {req_delta:.0f} istek üzerine, {timed_delta:.0f} zamanlanmış checkpoint. "
                        "İstek üzerine checkpoint baskınsa max_wal_size küçük kalıyor demektir; sık "
                        "checkpoint I/O dalgalanması yaratır."
                    ),
                    evidence={
                        "metric": "checkpoints_req / checkpoints_timed (dönem farkı)",
                        "value": req_delta,
                        "threshold": timed_delta,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("checkpoint_pressure", str(instance.id)),
                    recommendation="max_wal_size değerini artırmayı değerlendirin; parametre denetimindeki öneriyle birlikte okuyun.",
                    commands=["SHOW max_wal_size;", "SELECT * FROM pg_stat_bgwriter;"],
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

    if not rows:
        return SectionResult(
            key="resources",
            title="Kaynak kullanımı",
            status="unknown",
            summary="Dönem içinde metrik örneği yok.",
            unknown_reason="Kaynak kullanımı için ölçüm bulunamadı.",
        )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    peak = max((r["peak_utilization_pct"] or 0) for r in rows)
    return SectionResult(
        key="resources",
        title="Kaynak kullanımı",
        status=status,
        summary=f"{len(rows)} veritabanı; en yüksek bağlantı doluluğu %{peak:.0f}.",
        findings=findings,
        data={
            "instances": rows,
            "note": (
                "Sunucu seviyesi CPU/RAM/disk metrikleri dbace tarafından toplanmıyor; bu bölüm "
                "yalnızca veritabanı içi kaynak göstergelerini kapsar."
            ),
        },
    )


# --------------------------------------------------------------------------------------
# 7. Şema sağlığı
# --------------------------------------------------------------------------------------


@register_section
async def schema_section(ctx: ReportContext) -> SectionResult:
    """Şema sağlığı — büyüyen tablolar, kullanılmayan indexler (günlük şema anlık görüntüsünden)."""
    instance_ids = ctx.instance_ids()
    if not instance_ids:
        return SectionResult(
            key="schema", title="Şema sağlığı", status="unknown", summary="Kapsamda instance yok.",
            unknown_reason="Kapsama bağlı instance bulunamadı.",
        )

    rows = (
        await ctx.session.execute(
            select(SchemaObjectDailySample)
            .where(SchemaObjectDailySample.instance_id.in_(instance_ids))
            .order_by(SchemaObjectDailySample.day.asc())
        )
    ).scalars().all()

    if not rows:
        return SectionResult(
            key="schema",
            title="Şema sağlığı",
            status="unknown",
            summary="Henüz şema anlık görüntüsü alınmamış.",
            unknown_reason=(
                "Şema verisi günde bir kez toplanıyor (günlük rollup işi). En az bir gün geçmeden "
                "tablo/index büyümesi hakkında bir şey söylenemez."
            ),
        )

    by_object: dict[tuple, list] = {}
    for row in rows:
        by_object.setdefault((row.instance_id, row.object_kind, row.schema_name, row.object_name), []).append(row)

    growing: list[dict] = []
    unused_indexes: list[dict] = []
    findings: list[FindingDraft] = []

    for (instance_id, kind, schema_name, object_name), group in by_object.items():
        instance = ctx.instance_by_id(instance_id)
        latest = group[-1]
        first = group[0]
        growth = latest.size_bytes - first.size_bytes
        days = max((latest.day - first.day).days, 1)

        if kind == "index" and (latest.extra or {}).get("idx_scan") == 0:
            unused_indexes.append(
                {
                    "instance": instance.name if instance else instance_id,
                    "instance_id": instance_id,
                    "object": f"{schema_name}.{object_name}",
                    "size_bytes": latest.size_bytes,
                }
            )

        if growth > 0 and len(group) >= 2:
            growing.append(
                {
                    "instance": instance.name if instance else instance_id,
                    "instance_id": instance_id,
                    "kind": kind,
                    "object": f"{schema_name}.{object_name}",
                    "size_bytes": latest.size_bytes,
                    "growth_bytes": growth,
                    "growth_per_day": growth / days,
                    "days_observed": days,
                }
            )

    growing.sort(key=lambda g: g["growth_per_day"], reverse=True)
    unused_total = sum(i["size_bytes"] for i in unused_indexes)

    if unused_indexes:
        findings.append(
            FindingDraft(
                section="schema",
                severity="info",
                title=f"{len(unused_indexes)} kullanılmayan index ({_format_bytes(unused_total)})",
                detail=(
                    "Hiç taranmamış (idx_scan = 0) indexler hem disk kaplıyor hem de her yazma işlemine "
                    "maliyet ekliyor: "
                    + ", ".join(f"{i['object']} ({_format_bytes(i['size_bytes'])})" for i in unused_indexes[:5])
                ),
                evidence={
                    "metric": "pg_stat_user_indexes.idx_scan",
                    "value": len(unused_indexes),
                    "total_bytes": unused_total,
                    "measured_at": ctx.period_end.isoformat(),
                },
                fingerprint_parts=("unused_indexes", ctx.scope.scope_type, str(ctx.scope.scope_id or "all")),
                recommendation=(
                    "Instance detayındaki Şema sekmesinden DROP komutlarını alın; indexin gerçekten "
                    "gereksiz olduğunu (ör. sadece ayda bir çalışan bir rapor kullanmıyor mu) doğrulayın."
                ),
            )
        )

    for item in growing[:3]:
        if item["growth_per_day"] <= 0:
            continue
        findings.append(
            FindingDraft(
                section="schema",
                severity="info",
                title=f"{item['instance']}: {item['object']} hızlı büyüyor",
                detail=(
                    f"{item['days_observed']} günde {_format_bytes(item['growth_bytes'])} büyüdü "
                    f"(günlük ~{_format_bytes(item['growth_per_day'])}), şu an {_format_bytes(item['size_bytes'])}."
                ),
                evidence={
                    "metric": "schema_object_daily.size_bytes",
                    "value": item["size_bytes"],
                    "growth_per_day": item["growth_per_day"],
                    "days_observed": item["days_observed"],
                    "measured_at": ctx.period_end.isoformat(),
                },
                fingerprint_parts=("object_growth", str(item["instance_id"]), item["object"]),
                recommendation="Kapasite bölümündeki tahminle birlikte okuyun; arşivleme/partitioning değerlendirin.",
                related_object_type="instance",
                related_object_id=item["instance_id"],
            )
        )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    return SectionResult(
        key="schema",
        title="Şema sağlığı",
        status=status,
        summary=(
            f"{len(growing)} büyüyen nesne, {len(unused_indexes)} kullanılmayan index "
            f"({_format_bytes(unused_total)})."
        ),
        findings=findings,
        data={
            "growing_objects": growing[:20],
            "unused_indexes": unused_indexes[:20],
            "note": (
                "Bu bölüm günlük şema anlık görüntüsüne dayanır. Autovacuum gecikmesi ve tablo "
                "şişmesi (dead tuple) anlık olarak Şema sekmesinde ölçülür; geçmişe dönük "
                "saklanmadığı için burada raporlanmaz."
            ),
        },
    )


# --------------------------------------------------------------------------------------
# 9. Alarmlar
# --------------------------------------------------------------------------------------


@register_section
async def alerts_section(ctx: ReportContext) -> SectionResult:
    """Alarmlar — tetiklenenler, hâlâ açık olanlar, gürültü yapan kurallar."""
    instance_ids = ctx.instance_ids()
    if not instance_ids:
        return SectionResult(
            key="alerts", title="Alarmlar", status="unknown", summary="Kapsamda instance yok.",
            unknown_reason="Kapsama bağlı instance bulunamadı.",
        )

    # AlertEvent kuralın adını/eşiğini taşımaz (sadece rule_id + metric_value); okunabilir bir
    # rapor için kuralla birlikte çekiliyor.
    triggered = (
        await ctx.session.execute(
            select(AlertEvent, AlertRule)
            .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
            .where(
                AlertEvent.instance_id.in_(instance_ids),
                AlertEvent.triggered_at >= ctx.period_start,
                AlertEvent.triggered_at <= ctx.period_end,
            )
            .order_by(AlertEvent.triggered_at.asc())
        )
    ).all()

    still_open = (
        await ctx.session.execute(
            select(AlertEvent, AlertRule)
            .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
            .where(AlertEvent.instance_id.in_(instance_ids), AlertEvent.resolved_at.is_(None))
        )
    ).all()

    by_rule: dict[int, list] = {}
    rules: dict[int, AlertRule] = {}
    for event, rule in triggered:
        by_rule.setdefault(rule.id, []).append(event)
        rules[rule.id] = rule

    rule_rows: list[dict] = []
    findings: list[FindingDraft] = []
    for rule_id, group in by_rule.items():
        rule = rules[rule_id]
        noisy = len(group) >= NOISY_RULE_THRESHOLD
        rule_rows.append(
            {
                "rule_id": rule_id,
                "rule_name": rule.name,
                "metric": rule.metric,
                "severity": rule.severity,
                "threshold": rule.threshold,
                "count": len(group),
                "noisy": noisy,
                "first_at": as_utc(group[0].triggered_at).isoformat(),
                "last_at": as_utc(group[-1].triggered_at).isoformat(),
            }
        )
        if noisy:
            findings.append(
                FindingDraft(
                    section="alerts",
                    severity="info",
                    title=f"Gürültü yapan alarm kuralı: {rule.name} ({len(group)} tetikleme)",
                    detail=(
                        f"'{rule.name}' kuralı ({rule.metric} {rule.operator} {rule.threshold}) dönem içinde "
                        f"{len(group)} kez tetiklendi. Bu sıklık genelde eşiğin gerçek çalışma aralığına göre "
                        "çok dar olduğunu gösterir; her tetikleme gerçek bir olay değilse alarm körlüğü yaratır."
                    ),
                    evidence={
                        "metric": rule.metric,
                        "value": len(group),
                        "threshold": NOISY_RULE_THRESHOLD,
                        "rule_threshold": rule.threshold,
                        "rule_id": rule_id,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    fingerprint_parts=("noisy_rule", str(rule_id)),
                    recommendation="Alarmlar sayfasından kuralın eşiğini gerçek değer aralığına göre yeniden ayarlayın.",
                    related_object_type="alert_rule",
                    related_object_id=rule_id,
                )
            )

    for event, rule in still_open:
        # Sadece dönemden ÖNCE açılıp hâlâ kapanmamış olanlar "uzun süredir açık" sayılır;
        # bu dönemde açılmış bir alarm zaten yukarıdaki tetikleme listesinde.
        if as_utc(event.triggered_at) >= ctx.period_start:
            continue
        findings.append(
            FindingDraft(
                section="alerts",
                severity="critical" if rule.severity == "critical" else "warning",
                title=f"Uzun süredir açık alarm: {rule.name}",
                detail=(
                    f"{as_utc(event.triggered_at).date().isoformat()} tarihinden beri açık "
                    f"(ölçülen {event.metric_value}, eşik {rule.threshold}). {event.message}"
                ),
                evidence={
                    "metric": rule.metric,
                    "value": event.metric_value,
                    "threshold": rule.threshold,
                    "triggered_at": as_utc(event.triggered_at).isoformat(),
                    "measured_at": ctx.period_end.isoformat(),
                },
                fingerprint_parts=("open_alert", str(event.instance_id), str(rule.id)),
                recommendation="Alarmın kaynağını giderin ya da artık geçerli değilse kuralı güncelleyip olayı kapatın.",
                related_object_type="instance",
                related_object_id=event.instance_id,
            )
        )

    rule_rows.sort(key=lambda r: r["count"], reverse=True)
    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    return SectionResult(
        key="alerts",
        title="Alarmlar",
        status=status,
        summary=(
            f"{len(triggered)} tetikleme, {len(still_open)} hâlâ açık, "
            f"{sum(1 for r in rule_rows if r['noisy'])} gürültü yapan kural."
        ),
        findings=findings,
        data={"rules": rule_rows[:20], "triggered": len(triggered), "still_open": len(still_open)},
    )


# --------------------------------------------------------------------------------------
# 10. Kapasite
# --------------------------------------------------------------------------------------


@register_section
async def capacity_section(ctx: ReportContext) -> SectionResult:
    """Kapasite — disk dolma, wraparound, yaklaşan eşikler; güven aralığıyla."""
    instance_ids = ctx.instance_ids()
    if not instance_ids:
        return SectionResult(
            key="capacity", title="Kapasite", status="unknown", summary="Kapsamda instance yok.",
            unknown_reason="Kapsama bağlı instance bulunamadı.",
        )

    predictions = (
        await ctx.session.execute(
            select(PredictionInsight)
            .where(
                PredictionInsight.instance_id.in_(instance_ids),
                PredictionInsight.acknowledged_at.is_(None),
            )
            .order_by(PredictionInsight.created_at.desc())
        )
    ).scalars().all()

    if not predictions:
        return SectionResult(
            key="capacity",
            title="Kapasite",
            status="ok",
            summary="Açık kapasite tahmini yok.",
            data={"predictions": []},
            unknown_reason=None,
        )

    rows: list[dict] = []
    findings: list[FindingDraft] = []
    seen: set[tuple[int, str]] = set()

    for prediction in predictions:
        key = (prediction.instance_id, prediction.metric_key)
        if key in seen:
            continue  # aynı metrik için sadece en yeni tahmin
        seen.add(key)
        instance = ctx.instance_by_id(prediction.instance_id)
        interval = (
            f"{prediction.lower_bound:.1f} – {prediction.upper_bound:.1f}"
            if prediction.lower_bound is not None and prediction.upper_bound is not None
            else None
        )
        rows.append(
            {
                "instance_id": prediction.instance_id,
                "instance": instance.name if instance else prediction.instance_id,
                "metric_key": prediction.metric_key,
                "current_value": prediction.current_value,
                "predicted_value": prediction.predicted_value,
                "lower_bound": prediction.lower_bound,
                "upper_bound": prediction.upper_bound,
                "confidence": prediction.confidence,
                "severity": prediction.severity,
                "message": prediction.message,
                "horizon_days": round(prediction.horizon_minutes / 1440, 1),
            }
        )

        if prediction.severity in ("critical", "warning"):
            findings.append(
                FindingDraft(
                    section="capacity",
                    severity=prediction.severity,
                    title=f"{instance.name if instance else prediction.instance_id}: {prediction.metric_key} kapasite riski",
                    detail=prediction.message
                    + (f" %90 aralık: [{interval}]." if interval else " (güven aralığı hesaplanamadı)."),
                    evidence={
                        "metric": prediction.metric_key,
                        "value": prediction.current_value,
                        "predicted_value": prediction.predicted_value,
                        "lower_bound": prediction.lower_bound,
                        "upper_bound": prediction.upper_bound,
                        "threshold": prediction.threshold,
                        "confidence": prediction.confidence,
                        "horizon_days": round(prediction.horizon_minutes / 1440, 1),
                        "measured_at": as_utc(prediction.created_at).isoformat(),
                    },
                    fingerprint_parts=("capacity", str(prediction.instance_id), prediction.metric_key),
                    recommendation=prediction.recommendation
                    or "Tahminler sayfasındaki adım adım çözüm planına bakın.",
                    commands=[prediction.action] if prediction.action else [],
                    related_object_type="instance",
                    related_object_id=prediction.instance_id,
                    environment=_environment_of(instance) if instance else "prod",
                )
            )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    return SectionResult(
        key="capacity",
        title="Kapasite",
        status=status,
        summary=f"{len(rows)} açık kapasite tahmini; {sum(1 for r in rows if r['severity'] == 'critical')} kritik.",
        findings=findings,
        data={"predictions": rows},
    )



# --------------------------------------------------------------------------------------
# 8. Parametre denetimi
# --------------------------------------------------------------------------------------


async def _state_snapshots(ctx: ReportContext, kind: str) -> dict[int, list[DailyStateSnapshot]]:
    """instance_id → günlük durum fotoğrafları (eskiden yeniye)."""
    if not ctx.instance_ids():
        return {}
    rows = (
        await ctx.session.execute(
            select(DailyStateSnapshot)
            .where(DailyStateSnapshot.instance_id.in_(ctx.instance_ids()), DailyStateSnapshot.kind == kind)
            .order_by(DailyStateSnapshot.day.asc())
        )
    ).scalars().all()
    out: dict[int, list[DailyStateSnapshot]] = {}
    for row in rows:
        out.setdefault(row.instance_id, []).append(row)
    return out


@register_section
async def parameters_section(ctx: ReportContext) -> SectionResult:
    """Parametre denetimi — baseline sapmaları ve DÜN'e göre DEĞİŞEN parametreler.

    Değişen parametre tespiti bu bölümün asıl değeri: biri sunucuda elle `ALTER SYSTEM`
    çalıştırdıysa rapor bunu ertesi sabah gösterir.
    """
    snapshots = await _state_snapshots(ctx, "parameters")
    if not snapshots:
        return SectionResult(
            key="parameters",
            title="Parametre denetimi",
            status="unknown",
            summary="Parametre fotoğrafı henüz alınmamış.",
            unknown_reason=(
                "Parametreler günde bir kez (günlük rollup işi) kaydediliyor. En az bir gün "
                "geçmeden sapma veya değişiklik raporlanamaz."
            ),
        )

    findings: list[FindingDraft] = []
    deviations: list[dict] = []
    changes: list[dict] = []
    failed: list[str] = []

    for instance_id, rows in snapshots.items():
        instance = ctx.instance_by_id(instance_id)
        name = instance.name if instance else str(instance_id)
        latest = rows[-1]
        payload = latest.payload or {}
        if payload.get("error"):
            failed.append(name)
            continue

        for finding in payload.get("findings", []):
            if finding.get("severity") in ("ok", "unknown"):
                continue
            deviations.append(
                {
                    "instance_id": instance_id,
                    "instance": name,
                    "parameter": finding["name"],
                    "current_value": finding.get("current_value"),
                    "severity": finding["severity"],
                    "detail": finding.get("detail"),
                    "recommendation": finding.get("recommendation"),
                }
            )
            if finding["severity"] in ("critical", "high"):
                findings.append(
                    FindingDraft(
                        section="parameters",
                        severity="critical" if finding["severity"] == "critical" else "warning",
                        title=f"{name}: {finding['name']} baseline dışında",
                        detail=f"Mevcut değer {finding.get('current_value')} — {finding.get('detail')}",
                        evidence={
                            "metric": f"pg_settings.{finding['name']}",
                            "value": finding.get("current_value"),
                            "detail": finding.get("detail"),
                            "measured_at": payload.get("checked_at") or latest.day.isoformat(),
                        },
                        fingerprint_parts=("parameter_deviation", str(instance_id), finding["name"]),
                        recommendation=finding.get("recommendation"),
                        related_object_type="instance",
                        related_object_id=instance_id,
                        environment=_environment_of(instance) if instance else "prod",
                    )
                )

        # Bir önceki fotoğrafla karşılaştırma — elle yapılan değişiklikler burada yakalanır.
        previous_payload = next((r.payload for r in reversed(rows[:-1]) if (r.payload or {}).get("values")), None)
        if previous_payload:
            before = previous_payload.get("values") or {}
            after = payload.get("values") or {}
            for key in sorted(set(before) | set(after)):
                old, new = before.get(key), after.get(key)
                if old is None or new is None or old == new:
                    continue
                changes.append(
                    {
                        "instance_id": instance_id,
                        "instance": name,
                        "parameter": key,
                        "from": old,
                        "to": new,
                    }
                )
                findings.append(
                    FindingDraft(
                        section="parameters",
                        severity="warning",
                        title=f"{name}: {key} parametresi değişti",
                        detail=(
                            f"Önceki fotoğrafta '{old}', şimdi '{new}'. Planlı bir değişiklik değilse "
                            "kimin ve neden değiştirdiği araştırılmalı."
                        ),
                        evidence={
                            "metric": f"pg_settings.{key}",
                            "value": new,
                            "previous_value": old,
                            "measured_at": payload.get("checked_at") or latest.day.isoformat(),
                        },
                        fingerprint_parts=("parameter_changed", str(instance_id), key, str(old), str(new)),
                        recommendation=(
                            "Değişikliğin bilinçli olduğunu doğrulayın; değilse eski değere dönün ve "
                            "`ALTER SYSTEM` erişimini gözden geçirin."
                        ),
                        commands=[f"SHOW {key};", f"SELECT name, setting, source FROM pg_settings WHERE name = '{key}';"],
                        related_object_type="instance",
                        related_object_id=instance_id,
                        environment=_environment_of(instance) if instance else "prod",
                    )
                )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    summary = f"{len(deviations)} baseline sapması, {len(changes)} değişen parametre."
    if failed:
        summary += f" {len(failed)} instance için parametre okunamadı."
    return SectionResult(
        key="parameters",
        title="Parametre denetimi",
        status=status,
        summary=summary,
        findings=findings,
        data={"deviations": deviations[:40], "changes": changes[:40], "unreadable_instances": failed},
        unknown_reason=(
            f"{len(failed)} instance için parametre fotoğrafı alınamadı (bağlantı/yetki)."
            if failed
            else None
        ),
    )


# --------------------------------------------------------------------------------------
# 11. Ön koşullar
# --------------------------------------------------------------------------------------


@register_section
async def prerequisites_section(ctx: ReportContext) -> SectionResult:
    """Ön koşullar — eksik eklenti/yetki ve bu yüzden YAPILAMAYAN analizler.

    Yoksayılan (ignored) kontroller bulguya dönüşmez ama raporda ayrıca listelenir: kullanıcı
    "bu analiz neden yok?" sorusunun cevabını burada bulur.
    """
    snapshots = await _state_snapshots(ctx, "prerequisites")
    if not snapshots:
        return SectionResult(
            key="prerequisites",
            title="Ön koşullar",
            status="unknown",
            summary="Ön koşul fotoğrafı henüz alınmamış.",
            unknown_reason=(
                "Ön koşullar günde bir kez (günlük rollup işi) kaydediliyor. En az bir gün "
                "geçmeden raporlanamaz."
            ),
        )

    findings: list[FindingDraft] = []
    missing: list[dict] = []
    ignored_rows: list[dict] = []
    failed: list[str] = []

    for instance_id, rows in snapshots.items():
        instance = ctx.instance_by_id(instance_id)
        name = instance.name if instance else str(instance_id)
        payload = rows[-1].payload or {}
        if payload.get("error"):
            failed.append(name)
            continue

        ignored = set(payload.get("ignored") or [])
        for check in payload.get("checks", []):
            if check.get("status") == "ok":
                continue
            row = {
                "instance_id": instance_id,
                "instance": name,
                "key": check.get("key"),
                "name": check.get("name"),
                "status": check.get("status"),
                "impact": check.get("impact"),
                "fix": check.get("fix"),
                "ignored": check.get("key") in ignored,
            }
            if row["ignored"]:
                ignored_rows.append(row)
                continue
            if check.get("status") == "unknown":
                # "Kontrol edilemedi" bir eksiklik değil; sadece bilinmiyor — bulgu üretmiyoruz.
                continue
            missing.append(row)
            findings.append(
                FindingDraft(
                    section="prerequisites",
                    severity="warning" if check.get("severity") == "high" else "info",
                    title=f"{name}: {check.get('name')} eksik",
                    detail=f"{check.get('impact')}",
                    evidence={
                        "metric": f"prerequisite.{check.get('key')}",
                        "value": check.get("status"),
                        "detail": check.get("detail"),
                        "measured_at": rows[-1].day.isoformat(),
                    },
                    fingerprint_parts=("prerequisite", str(instance_id), str(check.get("key"))),
                    recommendation=(
                        "Aşağıdaki komutu çalıştırın; ortamınızda gerekmiyorsa Ön koşullar panelinden "
                        "bu kontrolü yoksayın."
                    ),
                    commands=[check["fix"]] if check.get("fix") else [],
                    related_object_type="instance",
                    related_object_id=instance_id,
                    environment=_environment_of(instance) if instance else "prod",
                )
            )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    summary = f"{len(missing)} eksik ön koşul"
    if ignored_rows:
        summary += f", {len(ignored_rows)} yoksayılmış kontrol"
    if failed:
        summary += f", {len(failed)} instance için okunamadı"
    return SectionResult(
        key="prerequisites",
        title="Ön koşullar",
        status=status,
        summary=summary + ".",
        findings=findings,
        data={
            "missing": missing[:40],
            "ignored": ignored_rows[:40],
            "unreadable_instances": failed,
            "note": (
                "Yoksayılan kontroller bulgu üretmez ama etkiledikleri analizler yine de "
                "çalışmaz — bu liste 'neden bu analiz yok?' sorusunun cevabıdır."
            ),
        },
        unknown_reason=f"{len(failed)} instance için ön koşul okunamadı." if failed else None,
    )

# --------------------------------------------------------------------------------------
# Özet bölümleri — diğer bölümlerin çıktısına bakarlar, SONRA çalışır ama raporda ÖNDE durur.
# --------------------------------------------------------------------------------------


def _all_drafts(results: list[SectionResult]) -> list[FindingDraft]:
    return [d for r in results for d in r.findings]


@register_summary_section
async def executive_summary_section(ctx: ReportContext, results: list[SectionResult]) -> SectionResult:
    """1. Yönetici özeti — en kritik 3-5 madde.

    Kendi bulgusunu ÜRETMEZ: diğer bölümlerin bulgularından en yüksek öncelikli olanları
    seçip listeler. Aksi halde aynı sorun iki kez (hem özet hem asıl bölüm) bulgu olarak
    sayılır, kritik sayısı şişerdi.
    """
    drafts = _all_drafts(results)
    ranked = sorted(
        drafts,
        key=lambda d: (_SEVERITY_RANK.get(d.severity, 0), _ENV_RANK.get(d.environment, 1.0)),
        reverse=True,
    )
    highlights = [
        {
            "severity": d.severity,
            "section": d.section,
            "title": d.title,
            "detail": d.detail[:400],
            "related_object_type": d.related_object_type,
            "related_object_id": d.related_object_id,
        }
        for d in ranked[:5]
        if d.severity in ("critical", "warning")
    ]

    unknown_sections = [r.title for r in results if r.status == "unknown"]
    critical = sum(1 for d in drafts if d.severity == "critical")
    warning = sum(1 for d in drafts if d.severity == "warning")

    if critical:
        status, summary = "critical", f"{critical} kritik, {warning} uyarı bulgusu var."
    elif warning:
        status, summary = "warning", f"Kritik bulgu yok; {warning} uyarı var."
    elif unknown_sections:
        status, summary = "info", "Bulgu yok, ancak bazı bölümler yeterli veri olmadığı için değerlendirilemedi."
    else:
        status, summary = "ok", "Bu dönemde dikkat gerektiren bir bulgu tespit edilmedi."

    return SectionResult(
        key="executive_summary",
        title="Yönetici özeti",
        status=status,
        summary=summary,
        data={
            "highlights": highlights,
            "critical_count": critical,
            "warning_count": warning,
            # Dürüstlük: değerlendirilemeyen bölümler özet seviyesinde de görünür, "sorunsuz"
            # izlenimi yaratılmaz (Faz 17 İŞ 6).
            "unknown_sections": unknown_sections,
            "period": {"start": ctx.period_start.isoformat(), "end": ctx.period_end.isoformat()},
            "scope": {"type": ctx.scope.scope_type, "id": ctx.scope.scope_id, "label": ctx.scope.label},
            "instance_count": len(ctx.instances),
        },
    )


@register_summary_section
async def changes_section(ctx: ReportContext, results: list[SectionResult]) -> SectionResult:
    """2. Dünden beri değişenler — düzelen / kötüleşen / yeni ortaya çıkan.

    Motorun fingerprint karşılaştırmasıyla AYNI mantığı kullanır (aynı `make_fingerprint`),
    böylece bu bölümde "yeni" görünen bir bulgu, kaydedilen satırda da `change_state="new"`
    olur — iki yerin farklı cevap vermesi mümkün değil.
    """
    if ctx.previous is None:
        return SectionResult(
            key="changes",
            title="Dünden beri değişenler",
            status="info",
            summary="Bu kapsam için ilk rapor — karşılaştırılacak önceki rapor yok.",
            data={"new": [], "resolved": [], "regressed": [], "ongoing": []},
        )

    current: dict[str, FindingDraft] = {}
    for draft in _all_drafts(results):
        current[make_fingerprint(draft.section, *draft.fingerprint_parts)] = draft

    new_items, regressed, ongoing = [], [], []
    for fingerprint, draft in current.items():
        prior = ctx.previous_findings.get(fingerprint)
        entry = {"section": draft.section, "title": draft.title, "severity": draft.severity}
        if prior is None:
            new_items.append(entry)
        elif _SEVERITY_RANK.get(draft.severity, 0) > _SEVERITY_RANK.get(prior.severity, 0):
            regressed.append({**entry, "previous_severity": prior.severity})
        else:
            ongoing.append({**entry, "open_since_days": prior.open_since_days + 1})

    resolved = [
        {"section": f.section, "title": f.title, "previous_severity": f.severity, "open_since_days": f.open_since_days}
        for fingerprint, f in ctx.previous_findings.items()
        if fingerprint not in current and f.change_state != "resolved"
    ]

    if regressed:
        status = "warning"
    elif new_items:
        status = "info"
    else:
        status = "ok"

    return SectionResult(
        key="changes",
        title="Dünden beri değişenler",
        status=status,
        summary=(
            f"{len(new_items)} yeni, {len(regressed)} kötüleşen, {len(resolved)} kapanan, "
            f"{len(ongoing)} süregelen bulgu."
        ),
        data={
            "new": new_items[:20],
            "regressed": regressed[:20],
            "resolved": resolved[:20],
            "ongoing": sorted(ongoing, key=lambda o: o["open_since_days"], reverse=True)[:20],
            "previous_report_id": ctx.previous.id,
            "previous_generated_at": as_utc(ctx.previous.generated_at).isoformat(),
        },
    )


@register_summary_section
async def known_issues_section(ctx: ReportContext, results: list[SectionResult]) -> SectionResult:
    """12. Bilinen konular — kabul edilmiş bulgular, kaç gündür açık.

    Bu bölüm de kendi bulgusunu üretmez; kabul edilmiş bulgular asıl bölümlerinde durur ve
    burada listelenir. Amaç, "susturulan" şeylerin gözden kaybolmaması.
    """
    acks = (
        await ctx.session.execute(select(FindingAcknowledgement).order_by(FindingAcknowledgement.acknowledged_at.desc()))
    ).scalars().all()
    if not acks:
        return SectionResult(
            key="known_issues",
            title="Bilinen konular",
            status="ok",
            summary="Kabul edilmiş (susturulmuş) bulgu yok.",
            data={"items": []},
        )

    now = datetime.now(UTC)
    current = {make_fingerprint(d.section, *d.fingerprint_parts): d for d in _all_drafts(results)}

    items = []
    for ack in acks:
        if ack.scope_id is not None and (
            ack.scope_type != ctx.scope.scope_type or ack.scope_id != ctx.scope.scope_id
        ):
            continue
        expired = ack.expires_at is not None and as_utc(ack.expires_at) <= now
        draft = current.get(ack.fingerprint)
        prior = ctx.previous_findings.get(ack.fingerprint)
        items.append(
            {
                "fingerprint": ack.fingerprint,
                "title": draft.title if draft else (prior.title if prior else "(bu raporda görünmüyor)"),
                "section": draft.section if draft else (prior.section if prior else None),
                "severity": draft.severity if draft else (prior.severity if prior else None),
                "acknowledged_by": ack.acknowledged_by,
                "acknowledged_at": as_utc(ack.acknowledged_at).isoformat(),
                "expires_at": as_utc(ack.expires_at).isoformat() if ack.expires_at else None,
                "expired": expired,
                "note": ack.note,
                "open_since_days": (prior.open_since_days + 1) if prior else 0,
                "still_present": draft is not None,
            }
        )

    expired_count = sum(1 for i in items if i["expired"])
    return SectionResult(
        key="known_issues",
        title="Bilinen konular",
        status="info" if expired_count else "ok",
        summary=(
            f"{len(items)} kabul edilmiş bulgu"
            + (f"; {expired_count} tanesinin kabul süresi dolmuş ve tekrar öne çıktı." if expired_count else ".")
        ),
        data={"items": items},
    )
