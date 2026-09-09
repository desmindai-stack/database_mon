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
from urllib.parse import quote

from sqlalchemy import select

from app.models import (
    AlertEvent,
    AlertRule,
    BackupProbe,
    BackupRecord,
    DailyStateSnapshot,
    DatabaseGroup,
    FindingAcknowledgement,
    GroupHealthSnapshot,
    DeadlockEvent,
    Instance,
    MetricSample,
    PredictionInsight,
    SchemaObjectDailySample,
    SlowQuerySample,
)
from app.domain.engines import DatabaseEngine
from app.domain.maintenance import OUTAGE_KIND_LABELS, OutageKind
from app.services.backup_health import assess_instance
from app.services.availability import (
    MIN_OUTAGE_SECONDS,
    OUTAGE_GAP_MULTIPLIER,
    as_utc,
    first_sample_at,
    outages_for_instance,
)
from app.services.collection import effective_collect_interval
from app.services.config_comparison import compare_group_from_snapshots
from app.services.advice import Advice, AdviceStep
from app.services.explain_service import validate_explainable
from app.services.finding_status import (
    STATUS_LABELS_TR,
    STATUS_OPEN,
    DecisionIndex,
    build_scope_membership,
    make_finding_type,
    resolve_status,
)
from app.config import settings
from app.services.blocking_history import recent_episodes
from app.services.maintenance import annotate_outages, occurrences_for_instance
from app.services.sla import (
    PERIOD_LABELS,
    evaluate_target,
    format_duration,
    targets_for_instances,
)
from app.services.health_report import (
    FindingDraft,
    ReportContext,
    SectionResult,
    make_fingerprint,
    register_section,
    register_summary_section,
)
from app.services.noise_settings import get_noise_settings
from app.services.query_diagnostics import diagnose_query
from app.services.slow_query_selection import select_slow_queries
from app.services.work_done import collect_work_done, summarize

# Kesinti eşikleri artık services/availability.py'de: SLA takibi de aynı sayıyı kullanıyor
# ve iki ayrı hesap, raporun "%99.95" derken SLA ekranının "%99.7" demesi demekti.
# İsimler geriye dönük uyumluluk için burada da görünür kalıyor (testler ve diğer bölümler
# buradan içe aktarıyor).


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
# Bulgu eşikleri artık yönetim ayarından geliyor (services/noise_settings.py) — gömülü
# sabitler her ortam için doğru olamazdı.
TOP_QUERIES = 10
# Bir alarm kuralının "gürültü yapıyor" sayılması için dönemdeki tetikleme sayısı.
NOISY_RULE_THRESHOLD = 10
# Dönemde bu kadar geçici dosya yazılmışsa work_mem sorgulanmaya değer. Birkaç kilobayt her
# veritabanında olur; eşiksiz bir kontrol kalıcı yanlış pozitif üretirdi (Faz 18 İŞ 5).
TEMP_BYTES_WARN = 1_048_576.0
# Bir servisin gerçekten kapalı sayılması için gereken ardışık "down" ölçümü.
SERVICE_DOWN_MIN_SAMPLES = 2
# Büyüme trendi için gereken en az gün sayısı — tek fotoğraftan büyüme çıkarılamaz.
SCHEMA_MIN_DAYS = 2
#: Kalan kesinti bütçesi, dönemin toplam bütçesinin bu oranının altına düşerse uyarı.
#: %25: bütçenin dörtte biri kaldığında hâlâ önlem alınabilir; daha düşük bir eşik
#: uyarıyı fiilen 'bütçe bitti' bulgusuyla aynı ana taşırdı.
SLA_BUDGET_WARNING_RATIO = 0.25
# Yedek değerlendirmesi için instance başına taranan en fazla kayıt. Yaş, süre ve boyut
# karşılaştırmaları son birkaç düzine kayıtla yapılıyor; tüm geçmişi belleğe almanın
# faydası yok.
BACKUP_RECORD_SCAN_LIMIT = 60

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
    """Sorgunun ANLAMLI baş kısmı (Faz 18 İŞ 4).

    Kesme kelime sınırında yapılıyor: ortasından ya da bir tanımlayıcının ortasından kesmek
    okunmayı zorlaştırıyordu. Tam metin her zaman `evidence["query"]` içinde duruyor ve
    arayüzde katlanabilir alanda gösteriliyor — bilgi kaybı yok.
    """
    collapsed = " ".join((query or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    boundary = cut.rfind(" ")
    # Boşluk çok başta kaldıysa (tek uzun jeton) sert kesme yapmak zorundayız.
    if boundary > limit * 0.6:
        cut = cut[:boundary]
    return cut.rstrip(" ,(") + "…"


def fact(label: str, value: str, tone: str = "neutral") -> dict:
    """Bulgunun sayısal özetinde tek satır. `tone`: neutral | good | bad."""
    return {"label": label, "value": value, "tone": tone}


def _format_bytes(value: float) -> str:
    step = 1024.0
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < step:
            return f"{amount:.1f} {unit}"
        amount /= step
    return f"{amount:.1f} PB"


def needs_more_days(have_days: float, need_days: float, what: str) -> str:
    """Faz 17 İŞ 6 dürüstlük kuralı: "veri yetersizse 'X gün daha veri gerekli' desin".

    Belirsiz bir "yeterli veri yok" cümlesi kullanıcıya ne zaman geri gelmesi gerektiğini
    söylemez; eksik gün sayısını yazmak bunu somutlaştırır.
    """
    missing = max(need_days - have_days, 0)
    if missing <= 0:
        return f"{what} için yeterli veri var."
    return (
        f"{what} için en az {need_days:.0f} günlük veri gerekiyor; şu an {have_days:.0f} gün var — "
        f"yaklaşık {missing:.0f} gün daha gerekli."
    )


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


#: Bloklama bulgusu için eşik. Tek bir oturumun kısa süre beklemesi normaldir; kilit
#: beklemesi veritabanının çalışma biçiminin parçasıdır. Bulgu üretmek için birden çok
#: oturumun etkilenmesi gerekiyor.
BLOCKING_FINDING_MIN_SESSIONS = 2
#: Bu sayının üstünde bloklama artık "kritik": tek bir oturum onlarca isteği durduruyor.
BLOCKING_CRITICAL_SESSIONS = 5

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
        outages = await outages_for_instance(
            ctx.session, instance, ctx.period_start, ctx.period_end
        )
        # PLANLI/PLANSIZ AYRIMI (Faz 28 İŞ 3). Bakım penceresi olmadan erişilebilirlik
        # sayıları dürüst değil: onaylanmış bir bakım, plansız bir arızayla aynı kefeye
        # girip aylık hedefi tek başına deliyordu.
        occurrences = await occurrences_for_instance(
            ctx.session, instance, ctx.period_start, ctx.period_end
        )
        outages = annotate_outages(outages, occurrences)
        first_sample = await first_sample_at(ctx.session, instance)

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
            # Faz 18 İŞ 5 denetimi: bu bulgu eskiden `continue`'dan SONRA yazılmıştı, yani
            # ulaşılamaz koddu ve hiç üretilmiyordu. Etkin ama hiç veri gelmeyen bir instance
            # sessizce görünmez kalıyordu — oysa bu, raporun söylemesi gereken en temel şey.
            if instance.enabled:
                findings.append(
                    FindingDraft(
                        section="availability",
                        severity="critical",
                        title=f"{instance.name}: hiç metrik toplanmamış",
                        detail=f"{instance.name} etkin ama tek bir ölçüm bile kaydedilmemiş.",
                        facts=[
                            fact("Ölçüm sayısı", "0", "bad"),
                            fact("Durum", "etkin"),
                        ],
                        evidence={
                            "metric": "metric_sample_count",
                            "value": 0,
                            "measured_at": ctx.period_end.isoformat(),
                        },
                        fingerprint_parts=("no_samples", str(instance.id)),
                        recommendation=(
                            "Bağlantı ayarlarını 'Bağlantı testi' ile doğrulayın; worker'ın çalıştığını "
                            "kontrol edin."
                        ),
                        related_object_type="instance",
                        related_object_id=instance.id,
                        link_hint=f"/instances/{instance.id}?tab=overview",
                        environment=_environment_of(instance),
                    )
                )
            continue

        total_down = sum(o["seconds"] for o in outages)
        planned_down = sum(o.get("planned_seconds", 0.0) for o in outages)
        unplanned_down = max(total_down - planned_down, 0.0)
        # Dönemin tamamı için değil, İZLENEBİLDİĞİ süre için yüzde hesaplanıyor: instance
        # dönemin ortasında eklendiyse ondan önceki zamanı "kesinti" saymak yanlış olurdu.
        observed_seconds = max(
            (ctx.period_end - max(as_utc(first_sample), ctx.period_start)).total_seconds(), 1.0
        )
        # ERİŞİLEBİLİRLİK YÜZDESİ PLANSIZ SÜREYE GÖRE. Planlı bakımı kesinti saymak,
        # müşteriyi kendi onayladığı bir çalışma yüzünden cezalandırmak olurdu. Planlı süre
        # kaybolmuyor: ayrı alan olarak raporlanıyor ve teknik dökümde görünüyor.
        uptime_pct = max(0.0, min(100.0, (1 - unplanned_down / observed_seconds) * 100))
        longest = max((o["seconds"] for o in outages), default=0.0)

        per_instance.append(
            {
                "instance_id": instance.id,
                "instance": instance.name,
                "uptime_pct": round(uptime_pct, 3),
                "outage_count": len(outages),
                "outage_seconds": round(total_down, 1),
                "planned_outage_seconds": round(planned_down, 1),
                "unplanned_outage_seconds": round(unplanned_down, 1),
                "planned_outage_count": sum(
                    1 for o in outages if o.get("kind") == str(OutageKind.PLANNED)
                ),
                "longest_outage_seconds": round(longest, 1),
                "observed_seconds": round(observed_seconds, 1),
                "outages": outages[:20],
                "environment": _environment_of(instance),
            }
        )

        ongoing = next((o for o in outages if o.get("ongoing")), None)
        # Bakım penceresi içindeki süregelen kesinti bir arıza DEĞİL: beklenen bir durum.
        # Bulguyu tamamen susturmuyoruz — bakımın hâlâ sürdüğünü bilmek de bir bilgi — ama
        # kritik olarak göstermek gece nöbetçisini boşuna ayağa kaldırırdı.
        ongoing_planned = bool(ongoing and ongoing.get("kind") == str(OutageKind.PLANNED))
        if ongoing is not None:
            # KÖK SEBEP BULGUSU (Faz 28 İŞ 2): dönem sonunda toplama hâlâ durmuş.
            #
            # Ayrı bir bulgu olması şart: "dönem içinde 3 kesinti oldu" ile "şu anda hâlâ
            # erişilemiyor" çok farklı iki durum ve ikincisi bugün müdahale gerektiriyor.
            # Bu bulgu, o veritabanına ait diğer bölümlerin bastırılmasını da tetikliyor
            # (services/finding_dependencies.py).
            findings.append(
                FindingDraft(
                    section="availability",
                    severity="info" if ongoing_planned else "critical",
                    title=(
                        f"{instance.name}: bakım penceresinde, veri toplanmıyor"
                        if ongoing_planned
                        else f"{instance.name}: veri toplama durmuş, sürüyor"
                    ),
                    detail=(
                        f"{instance.name} için son ölçüm {_fmt_duration(ongoing['seconds'])} önce "
                        "alındı ve dönem sonunda hâlâ veri gelmiyordu. Bu veritabanı hakkındaki "
                        "diğer analizler canlı veriye değil, saklanmış eski fotoğraflara dayanır."
                        + (
                            " Kesinti tanımlı bir bakım penceresine denk geliyor; beklenen bir "
                            "durum olarak işaretlendi."
                            if ongoing_planned
                            else ""
                        )
                    ),
                    facts=[
                        fact("Son ölçümden bu yana", _fmt_duration(ongoing["seconds"]), "bad"),
                        fact("Son ölçüm", ongoing["start"][11:16]),
                        fact("Durum", "sürüyor", "bad"),
                    ],
                    evidence={
                        "metric": "metric_sample_trailing_gap",
                        "value": round(ongoing["seconds"], 1),
                        "threshold": max(
                            effective_collect_interval(instance) * OUTAGE_GAP_MULTIPLIER,
                            MIN_OUTAGE_SECONDS,
                        ),
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    note=(
                        "Bu ölçüm 'dbace veri toplayamıyor' demektir; veritabanının kapalı "
                        "olduğunu tek başına kanıtlamaz (worker duruşu, ağ kopması ya da "
                        "kimlik bilgisi sorunu da aynı sonucu verir)."
                    ),
                    fingerprint_parts=("unreachable_now", str(instance.id)),
                    recommendation=(
                        "Bağlantıyı test edin ve worker'ın çalıştığını doğrulayın; bu düzelmeden "
                        "bu veritabanı için üretilen diğer bulgular güncel değildir."
                    ),
                    advice=Advice(
                        title="Veri toplamayı yeniden çalışır hale getirin",
                        why=(
                            "Toplama durduğu sürece dbace bu veritabanı hakkında hiçbir güncel şey "
                            "söyleyemez: performans, yedek, kapasite ve parametre bulgularının hepsi "
                            "eski fotoğraftan üretilir. Ayrıca gerçekten bir kesinti yaşanıyorsa "
                            "uygulama da aynı anda etkileniyor demektir."
                        ),
                        steps=[
                            AdviceStep(
                                "Bağlantıyı dbace üzerinden test edin (Veritabanları → Düzenle → "
                                "Bağlantı testi); hata mesajı sorunun ağ mı, kimlik bilgisi mi, "
                                "servis mi olduğunu söyler."
                            ),
                            AdviceStep(
                                "Sunucuda veritabanı servisinin durumuna bakın.",
                                "systemctl status postgresql\njournalctl -u postgresql --since '2 hours ago' | tail -50",
                            ),
                            AdviceStep(
                                "Servis ayaktaysa dbace worker'ının çalıştığını doğrulayın — boşluk "
                                "izleme tarafından da kaynaklanabilir."
                            ),
                        ],
                        cautions=[
                            "Bu bulgu veritabanının kapalı olduğunu kanıtlamaz; önce izleme "
                            "tarafını elemek daha hızlı sonuç verir.",
                            "Bu bulgu açıkken aynı veritabanı için üretilen diğer bulgular "
                            "bastırılır — düzeldiğinde tekrar değerlendirilecekler.",
                        ],
                        verification=(
                            "-- Bir sonraki toplama turunda ölçüm gelmeye başlamalı; "
                            "instance detay sayfasında 'son toplama' zamanı güncellenir."
                        ),
                    ),
                    related_object_type="instance",
                    related_object_id=instance.id,
                    link_hint=f"/instances/{instance.id}?tab=overview",
                    environment=_environment_of(instance),
                )
            )

        # Süregelen kesinti ayrı bulguya çıktığı için buradaki "dönem içinde N kesinti"
        # bulgusu yalnızca KAPANMIŞ kesintileri anlatıyor; ikisini aynı bulguda toplamak
        # "geçmişte oldu" ile "şu anda sürüyor"u aynı cümleye sıkıştırırdı.
        # Tamamen planlı kesintiler bulguya DÖNÜŞMÜYOR: onaylanmış bir bakımı her raporda
        # bulgu olarak göstermek, bulgu listesini takvim haline getirirdi. Sayısı ve süresi
        # bölüm verisinde ve teknik dökümde duruyor.
        closed = [
            o
            for o in outages
            if not o.get("ongoing") and o.get("kind") != str(OutageKind.PLANNED)
        ]
        if closed:
            worst = max(closed, key=lambda o: o["seconds"])
            closed_total = sum(o["seconds"] for o in closed)
            closed_longest = max(o["seconds"] for o in closed)
            severity = "critical" if closed_longest >= 300 or len(closed) >= 5 else "warning"
            findings.append(
                FindingDraft(
                    section="availability",
                    severity=severity,
                    title=f"{instance.name}: veri toplanamayan {len(closed)} dönem",
                    detail=f"{instance.name} için dönem içinde {len(closed)} kez veri toplanamadı.",
                    facts=[
                        fact("Toplam kesinti", _fmt_duration(closed_total), "bad"),
                        fact("En uzunu", _fmt_duration(closed_longest), "bad"),
                        fact("En uzun kesintinin başlangıcı", worst["start"][11:16]),
                        fact("Erişilebilirlik", f"%{uptime_pct:.2f}", "bad" if uptime_pct < 99 else "good"),
                    ],
                    note=(
                        "Bu ölçüm 'dbace veri toplayamadı' demektir; veritabanının kapalı olduğunu "
                        "tek başına kanıtlamaz (worker duruşu, ağ kopması veya kimlik bilgisi sorunu "
                        "da aynı boşluğu yaratır)."
                    ),
                    evidence={
                        "metric": "metric_sample_gap",
                        # Süregelen kesinti ayrı bulguda; buradaki sayılar KAPANMIŞ kesintilere ait.
                        "outage_count": len(closed),
                        "total_seconds": round(closed_total, 1),
                        "longest_seconds": round(closed_longest, 1),
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
                    advice=Advice(
                        title="Kesinti saatlerindeki kaydı inceleyip kaynağı belirleyin",
                        why=(
                            "Veri toplanamayan her dönem, o sürede veritabanının gerçekten erişilebilir olup "
                            "olmadığını bilmediğimiz anlamına gelir; tekrarlıyorsa uygulama da aynı "
                            "kesintileri yaşıyor olabilir."
                        ),
                        steps=[
                            AdviceStep(
                                f"Sunucuda PostgreSQL servisinin o saatlerdeki durumunu kontrol edin "
                                f"(en uzun kesintinin başlangıcı: {worst['start'][11:16]}).",
                                "systemctl status postgresql\n"
                                "journalctl -u postgresql --since '1 day ago' | tail -100",
                            ),
                            AdviceStep(
                                "dbace worker'ının aynı saatte çalışıp çalışmadığını doğrulayın — boşluk "
                                "veritabanından değil izleme tarafından da kaynaklanabilir.",
                            ),
                            AdviceStep(
                                "Kimlik bilgisi ya da ağ sorunu ihtimalini elemek için bağlantıyı test edin "
                                "(Instances → Düzenle → Bağlantı testi).",
                            ),
                        ],
                        cautions=["Bu bulgu tek başına veritabanının kapalı olduğunu kanıtlamaz."],
                        verification=(
                            "-- Bir sonraki rapor bu instance için kesinti göstermiyorsa sorun giderilmiştir."
                        ),
                    ),
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

    measured = [p for p in per_instance if p["uptime_pct"] is not None]
    overall_uptime = round(sum(p["uptime_pct"] for p in measured) / len(measured), 3) if measured else None
    total_outages = sum(p["outage_count"] for p in per_instance)
    planned_outages = sum(p.get("planned_outage_count", 0) for p in per_instance)
    unplanned_outages = total_outages - planned_outages

    if not measured:
        status = "unknown"
        summary = "Hiçbir instance için ölçüm yok — erişilebilirlik hesaplanamadı."
    elif total_outages == 0:
        status = "ok"
        summary = f"{len(measured)} veritabanının tamamından kesintisiz veri toplandı."
    elif unplanned_outages == 0:
        # Yalnızca planlı bakım: durum "sorunsuz" ama sessiz değil — bakımın yapıldığı
        # söyleniyor, çünkü müşteri sayının neden %100 olmadığını sorabilir.
        status = "ok"
        summary = (
            f"{planned_outages} planlı bakım kesintisi dışında kesinti yok; "
            f"erişilebilirlik %{overall_uptime}."
        )
    else:
        worst_uptime = min(p["uptime_pct"] for p in measured)
        status = "critical" if worst_uptime < 99.0 else "warning"
        summary = (
            f"{unplanned_outages} plansız kesinti tespit edildi"
            + (f" ({planned_outages} planlı bakım hariç)" if planned_outages else "")
            + f"; ortalama erişilebilirlik %{overall_uptime}."
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
            "planned_outages": planned_outages,
            "unplanned_outages": unplanned_outages,
            "outage_kind_labels": dict(OUTAGE_KIND_LABELS),
            "instances": per_instance,
            "no_data_instances": no_data,
            "method": (
                "Erişilebilirlik, toplanan metrik örnekleri arasındaki boşluklardan türetilir "
                f"(toplama aralığının {OUTAGE_GAP_MULTIPLIER} katından uzun boşluk = kesinti). "
                "Tanımlı bakım penceresine denk gelen süre PLANLI sayılır ve yüzdeye "
                "girmez; planlı süre ayrıca raporlanır."
            ),
        },
        unknown_reason=None if measured else "Dönem içinde hiç metrik örneği yok.",
    )


# --------------------------------------------------------------------------------------
# 3b. SLA takibi (Faz 28 İŞ 3b)
# --------------------------------------------------------------------------------------


@register_section
async def sla_section(ctx: ReportContext) -> SectionResult:
    """SLA takibi — taahhüt tutuyor mu, ne kadar bütçe kaldı.

    Erişilebilirlik bölümü "ne oldu" diyor; bu bölüm "taahhüde göre nerede duruyoruz"
    diyor. İkisi ayrı çünkü hedef olmadan erişilebilirlik sayısı bir bilgi ama bir KARAR
    değil: %99.7 iyi mi kötü mü, ancak taahhüde göre söylenebilir.
    """
    if not ctx.instances:
        return SectionResult(
            key="sla", title="SLA takibi", status="unknown",
            summary="Bu kapsamda izlenen veritabanı yok.",
            unknown_reason="Kapsama bağlı etkin instance bulunamadı.",
        )

    targets = await targets_for_instances(ctx.session, ctx.instances)
    if not targets:
        # "SLA tutuyor" DEMİYORUZ: hedef tanımlı değilse tutup tutmadığı bilinemez.
        return SectionResult(
            key="sla", title="SLA takibi", status="unknown",
            summary="Bu kapsam için tanımlı SLA hedefi yok.",
            unknown_reason=(
                "Erişilebilirlik hedefi tanımlanmadan SLA takibi yapılamaz. Yönetim → SLA "
                "hedefleri bölümünden müşteri ya da uygulama bazında hedef tanımlayın "
                "(ör. %99.9, aylık)."
            ),
        )

    findings: list[FindingDraft] = []
    rows: list[dict] = []
    for target in targets:
        status_row = await evaluate_target(ctx.session, target, ctx.period_end)
        rows.append(status_row.to_dict())
        if not status_row.measured:
            continue

        budget = status_row.remaining_budget_seconds or 0.0
        if status_row.already_lost:
            # MATEMATİKSEL KAYIP: kalan süre kusursuz geçse bile hedef tutmuyor. Bunu dönem
            # ortasında bilmek, dönem sonunda öğrenmekten bambaşka bir yönetim kararı üretir.
            findings.append(
                FindingDraft(
                    section="sla",
                    severity="critical",
                    title=f"{status_row.scope_label}: SLA hedefi bu dönem tutturulamayacak",
                    detail=(
                        f"Hedef %{status_row.target_pct}, dönem sonuna kadar kalan süre "
                        "kesintisiz geçse bile ulaşılabilecek en iyi oran "
                        f"%{status_row.best_case_pct}. Yani hedef matematiksel olarak "
                        "kaybedilmiş durumda."
                    ),
                    evidence={
                        "metric": "sla_best_case_pct",
                        "value": status_row.best_case_pct,
                        "threshold": status_row.target_pct,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    facts=[
                        fact("Hedef", f"%{status_row.target_pct}"),
                        fact("Gerçekleşen", f"%{status_row.achieved_pct}", "bad"),
                        fact("En iyi durum", f"%{status_row.best_case_pct}", "bad"),
                        fact("Plansız kesinti", format_duration(status_row.unplanned_seconds), "bad"),
                        fact("En kötü veritabanı", status_row.worst_instance or "—"),
                    ],
                    recommendation=(
                        "Kalan dönemde plansız kesinti riskini düşürün ve dönem sonu için "
                        "müşteriye açıklama hazırlayın."
                    ),
                    advice=_sla_advice(status_row, lost=True),
                    fingerprint_parts=("sla_lost", status_row.scope_type, str(status_row.scope_id or 0)),
                    related_object_type=(
                        status_row.scope_type if status_row.scope_type != "global" else None
                    ),
                    related_object_id=status_row.scope_id,
                )
            )
        elif budget <= 0:
            findings.append(
                FindingDraft(
                    section="sla",
                    severity="critical",
                    title=f"{status_row.scope_label}: kesinti bütçesi tükendi",
                    detail=(
                        f"Hedef %{status_row.target_pct}; bu dönem için ayrılan kesinti bütçesi "
                        "tükendi. Bundan sonraki her plansız kesinti taahhüdü doğrudan ihlal "
                        "eder."
                    ),
                    evidence={
                        "metric": "sla_remaining_budget_seconds",
                        "value": round(budget, 1),
                        "threshold": 0,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    facts=[
                        fact("Hedef", f"%{status_row.target_pct}"),
                        fact("Gerçekleşen", f"%{status_row.achieved_pct}", "bad"),
                        fact("Kalan bütçe", format_duration(budget), "bad"),
                    ],
                    recommendation="Bakım planlarını dönem sonuna kadar erteleyin.",
                    advice=_sla_advice(status_row, lost=False),
                    fingerprint_parts=("sla_budget", status_row.scope_type, str(status_row.scope_id or 0)),
                    related_object_type=(
                        status_row.scope_type if status_row.scope_type != "global" else None
                    ),
                    related_object_id=status_row.scope_id,
                )
            )
        elif budget < SLA_BUDGET_WARNING_RATIO * (
            (status_row.period_end - status_row.period_start).total_seconds()
            * (1 - status_row.target_pct / 100)
        ):
            findings.append(
                FindingDraft(
                    section="sla",
                    severity="warning",
                    title=f"{status_row.scope_label}: kesinti bütçesinin çoğu tüketildi",
                    detail=(
                        f"Hedef %{status_row.target_pct}; kalan kesinti bütçesi "
                        f"{format_duration(budget)}. Planlanacak her bakım bu bütçeden düşer."
                    ),
                    evidence={
                        "metric": "sla_remaining_budget_seconds",
                        "value": round(budget, 1),
                        "threshold": 0,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    facts=[
                        fact("Hedef", f"%{status_row.target_pct}"),
                        fact("Gerçekleşen", f"%{status_row.achieved_pct}"),
                        fact("Kalan bütçe", format_duration(budget), "bad"),
                    ],
                    recommendation="Kalan dönemde plansız kesinti riskini azaltın.",
                    advice=_sla_advice(status_row, lost=False),
                    fingerprint_parts=("sla_budget_low", status_row.scope_type, str(status_row.scope_id or 0)),
                    related_object_type=(
                        status_row.scope_type if status_row.scope_type != "global" else None
                    ),
                    related_object_id=status_row.scope_id,
                )
            )

    measured_rows = [r for r in rows if r["measured"]]
    if not measured_rows:
        return SectionResult(
            key="sla", title="SLA takibi", status="unknown",
            summary="SLA hedefi tanımlı ama ölçüm yok.",
            data={"targets": rows},
            unknown_reason=(
                "Dönem içinde hiç ölçüm alınmamış; erişilebilirlik hesaplanamıyor. Bu, "
                "sistemin ayakta olduğu anlamına GELMEZ."
            ),
        )

    breached = [r for r in measured_rows if r["met"] is False]
    status = _worst_status([f.severity for f in findings]) if findings else ("warning" if breached else "ok")
    summary = (
        f"{len(breached)} hedef karşılanmıyor ({len(measured_rows)} hedef izleniyor)."
        if breached
        else f"{len(measured_rows)} SLA hedefinin tamamı karşılanıyor."
    )
    return SectionResult(
        key="sla", title="SLA takibi", status=status, summary=summary,
        findings=findings, data={"targets": rows},
    )


def _sla_advice(status_row, *, lost: bool) -> Advice:
    """SLA bulgusu için beş parçalı öneri.

    Öneriler teknik değil YÖNETSEL: SLA'yı kurtaran şey bir komut değil, kalan dönemde
    risk almamak ve müşteriyle doğru zamanda konuşmak.
    """
    return Advice(
        title=(
            "Dönem sonu için müşteri iletişimini planlayın"
            if lost
            else "Kalan kesinti bütçesini koruyun"
        ),
        why=(
            f"{status_row.scope_label} için taahhüt %{status_row.target_pct} "
            f"({PERIOD_LABELS.get(status_row.period, status_row.period).lower()}). "
            + (
                "Kalan süre kesintisiz geçse bile hedefe ulaşılamıyor; sürpriz bir dönem sonu "
                "raporu, sorunun kendisinden daha çok güven kaybettirir."
                if lost
                else f"Kalan bütçe {format_duration(status_row.remaining_budget_seconds or 0)}; "
                "bu bütçe tükendikten sonra her kesinti doğrudan ihlal demek."
            )
        ),
        steps=[
            AdviceStep(
                "Dönem içindeki plansız kesintilerin kök nedenlerini gözden geçirin; "
                "tekrar edenler öncelikli."
            ),
            AdviceStep(
                "Planlanmış bakımları dönem sonuna kadar erteleyin ya da bakım penceresi "
                "tanımlayarak planlı hale getirin — planlı süre bütçeden düşmez."
            ),
            AdviceStep(
                "Riskli değişiklikleri (sürüm yükseltme, şema göçü) bir sonraki döneme alın."
            ),
        ]
        + (
            [
                AdviceStep(
                    "Müşteriye dönem sonunu beklemeden bilgi verin; nedeni, alınan önlemi ve "
                    "telafi planını birlikte sunun."
                )
            ]
            if lost
            else []
        ),
        cautions=[
            "Bu ölçüm VERİTABANI erişilebilirliğidir, uygulama erişilebilirliği değil: "
            "replikalı bir kümede bir düğümün düşmesi uygulama için kesinti olmayabilir.",
            "Bakım penceresi geçmişe dönük tanımlanamaz; geçmiş bir kesintiyi sonradan "
            "planlı yapmak mümkün değil.",
        ],
        verification=(
            "Bir sonraki raporda 'kalan kesinti bütçesi' değerinin azalmaması gerekir."
        ),
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
            # Faz 18 İŞ 5 denetimi: tek ölçümlük "down" bir probe hıçkırığı olabilir; gerçek
            # bir kesinti en az iki ardışık ölçümde görünür.
            if count < SERVICE_DOWN_MIN_SAMPLES:
                continue
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
            checked_at = as_utc(snapshot.checked_at) if snapshot.checked_at else None
            # Anlık görüntü rapor döneminin DIŞINDAysa bunu açıkça söylüyoruz.
            snapshot_note = (
                "Bu bilgi grubun anlık sağlık görüntüsünden geliyor; dönem boyunca sürekli "
                "izlenmiş bir değer değil."
            )
            if checked_at is not None and not (ctx.period_start <= checked_at <= ctx.period_end):
                snapshot_note += (
                    f" Görüntü {checked_at.strftime('%d.%m.%Y %H:%M')} tarihli, yani rapor "
                    "döneminin dışında."
                )
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
                        note=snapshot_note,
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
                        note=snapshot_note,
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
                        note=snapshot_note,
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
# 4b. Yedek durumu (Faz 28 İŞ 1b)
# --------------------------------------------------------------------------------------


@register_section
async def backup_section(ctx: ReportContext) -> SectionResult:
    """Yedek durumu — "son yedek ne zaman alındı" sorusunun cevabı.

    Bölüm sıralamada erişilebilirlik ve cluster'ın hemen ardında: bir olay sonrası sorulan
    ilk üç sorudan biri bu ve cevabın raporun dibinde aranmaması gerekiyor.

    DÜRÜSTLÜK KURALI: bulgu bulunamadığında "yedek alınıyor" varsayılmıyor. Bölüm üç ayrı
    durumu ayırıyor — yedek var ve güncel, yedek yok, dbace göremedi. Üçüncüsünü ikincisi
    gibi göstermek gerçekten yedeği olan kurumu paniğe sürükler; ikincisini birincisi gibi
    göstermek ise felaket anında geri dönüşü olmayan bir sürprize.
    """
    instances = [i for i in ctx.instances if i.engine != str(DatabaseEngine.MONGODB)]
    if not instances:
        return SectionResult(
            key="backup", title="Yedek durumu", status="unknown",
            summary="Kapsamda yedek izlenebilen veritabanı yok.",
            unknown_reason=(
                "Kapsamdaki veritabanları MongoDB ya da kapsam boş. MongoDB için yedek izleme "
                "desteklenmiyor: mongodump/Ops Manager yedekleri veritabanının kendi "
                "kataloğunda iz bırakmıyor."
            ),
        )

    if not settings.backup_monitoring_enabled:
        # "Yedek yok" ile "yedek izleme kapalı" karıştırılamaz. Kapalıyken bölümü "sorunsuz"
        # göstermek, izlemenin kapalı olduğunu bilmeyen birine sahte güvence verirdi.
        return SectionResult(
            key="backup", title="Yedek durumu", status="unknown",
            summary="Yedek izleme kapalı.",
            unknown_reason=(
                "Yedek izleme kapalı (BACKUP_MONITORING_ENABLED=false). Bu, yedek alınmadığı "
                "anlamına GELMEZ — dbace hiç bakmadı demektir."
            ),
        )

    instance_ids = [i.id for i in instances]
    probes = {
        p.instance_id: p
        for p in (
            await ctx.session.execute(
                select(BackupProbe).where(BackupProbe.instance_id.in_(instance_ids))
            )
        ).scalars().all()
    }
    # Kayıtlar instance başına sınırlanıyor: yaş, süre ve boyut karşılaştırmaları için son
    # birkaç düzine kayıt yeterli, tüm geçmişi belleğe almanın faydası yok.
    records: dict[int, list] = {i: [] for i in instance_ids}
    rows = (
        await ctx.session.execute(
            select(BackupRecord)
            .where(BackupRecord.instance_id.in_(instance_ids))
            .order_by(BackupRecord.started_at.desc())
            .limit(BACKUP_RECORD_SCAN_LIMIT * max(len(instance_ids), 1))
        )
    ).scalars().all()
    for row in rows:
        bucket = records.setdefault(row.instance_id, [])
        if len(bucket) < BACKUP_RECORD_SCAN_LIMIT:
            bucket.append(row)

    findings: list[FindingDraft] = []
    instance_rows: list[dict] = []
    assessments: list = []

    for instance in instances:
        probe = probes.get(instance.id)
        assessment = assess_instance(
            instance, records.get(instance.id, []), probe, now=ctx.period_end
        )
        assessments.append(assessment)
        instance_rows.append(
            {
                "instance_id": instance.id,
                "instance_name": instance.name,
                "engine": instance.engine,
                "status": assessment.status,
                "sla_ok": assessment.sla_ok,
                "detected": assessment.detected,
                "conclusive": assessment.conclusive,
                "last_full_at": (
                    assessment.last_full_at.isoformat() if assessment.last_full_at else None
                ),
                "last_full_age_hours": (
                    round(assessment.last_full_age_hours, 2)
                    if assessment.last_full_age_hours is not None
                    else None
                ),
                "last_log_at": (
                    assessment.last_log_at.isoformat() if assessment.last_log_at else None
                ),
                "last_log_age_hours": (
                    round(assessment.last_log_age_hours, 2)
                    if assessment.last_log_age_hours is not None
                    else None
                ),
                "methods_checked": assessment.methods_checked,
                "methods_found": assessment.methods_found,
                "running": assessment.running,
                # Always On: yedeği hangi replika alıyor. Birden fazla düğüm görünüyorsa
                # geri dönüş anında hangi sunucudaki dosyanın gerekli olduğu belirsizleşir.
                "backup_servers": assessment.backup_servers,
                "issue_count": len(assessment.issues),
                "probed_at": (
                    as_utc(probe.probed_at).isoformat() if probe is not None and probe.probed_at else None
                ),
            }
        )
        for issue in assessment.issues:
            findings.append(
                FindingDraft(
                    section="backup",
                    severity=issue.severity,
                    title=f"{instance.name}: {issue.title}",
                    detail=issue.detail,
                    evidence=issue.evidence,
                    facts=issue.facts,
                    recommendation=issue.advice.title,
                    advice=issue.advice,
                    # Fingerprint'e ölçülen değer GİRMİYOR (bkz. make_fingerprint): yedek yaşı
                    # her gün değişir ve değer hash'e girseydi bulgu her gün "yeni" görünür,
                    # "kaç gündür açık" sayacı hiç ilerlemezdi.
                    fingerprint_parts=("backup", str(instance.id), issue.key),
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

    # Yedeği birden fazla düğümden alan Always On grupları: teknik raporda bilgi notu.
    for assessment in assessments:
        if len(assessment.backup_servers) > 1:
            findings.append(
                FindingDraft(
                    section="backup",
                    severity="info",
                    title=f"{assessment.instance_name}: yedekler birden fazla düğümden alınıyor",
                    detail=(
                        "Yedek kayıtları "
                        + ", ".join(assessment.backup_servers)
                        + " düğümlerinden geliyor. Always On'da yedek tercihinin bir replikaya "
                        "sabitlenmesi olağandır; birden fazla düğümden yedek alınması geri "
                        "dönüş anında hangi sunucudaki dosyanın gerektiğini belirsizleştirir."
                    ),
                    evidence={
                        "metric": "backup_source_nodes",
                        "value": len(assessment.backup_servers),
                        "threshold": 1,
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    facts=[
                        {"label": "Düğümler", "value": ", ".join(assessment.backup_servers), "tone": "neutral"},
                    ],
                    advice=Advice(
                        title="Yedek tercihini tek bir replikaya sabitleyin",
                        why=(
                            "Yedeklerin hangi düğümden alındığı belirsizse geri dönüş anında "
                            "dosyaların hangi sunucuda olduğu da belirsizdir; kurtarma süresi "
                            "dosya aramakla uzar."
                        ),
                        steps=[
                            AdviceStep(
                                "Availability group'un yedek tercihini kontrol edin.",
                                "SELECT name, automated_backup_preference_desc FROM sys.availability_groups;",
                            ),
                            AdviceStep(
                                "Yedek işlerinde `sys.fn_hadr_backup_is_preferred_replica` "
                                "kontrolünün kullanıldığını doğrulayın.",
                                "SELECT sys.fn_hadr_backup_is_preferred_replica(N'<veritabani>');",
                            ),
                        ],
                        cautions=[
                            "Tercihi değiştirmek yedek zincirini etkilemez ama yedek dosyalarının "
                            "yeni düğümden erişilebilir olduğunu doğrulayın.",
                        ],
                        verification=(
                            "SELECT DISTINCT server_name FROM msdb.dbo.backupset "
                            "WHERE backup_start_date > DATEADD(day, -7, GETDATE());"
                        ),
                    ),
                    fingerprint_parts=("backup", str(assessment.instance_id), "multi_source_node"),
                    related_object_type="instance",
                    related_object_id=assessment.instance_id,
                )
            )

    unknown = [a for a in assessments if a.status == "unknown"]
    protected = [a for a in assessments if a.sla_ok is True]
    breached = [a for a in assessments if a.sla_ok is False]

    if len(unknown) == len(assessments):
        # Hiçbiri belirlenemedi: bölümün tamamı "unknown". "ok" demek yalan olurdu.
        return SectionResult(
            key="backup", title="Yedek durumu", status="unknown",
            summary=f"{len(unknown)} veritabanının yedek durumu belirlenemedi.",
            findings=findings,
            data={"instances": instance_rows},
            unknown_reason=(
                "Yedek kaynaklarına erişilemedi. Bu, yedek alınmadığı anlamına GELMEZ; "
                "bulgulardaki adımlar kaynağın nasıl görünür hale getirileceğini anlatıyor."
            ),
        )

    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    summary_parts = []
    if protected:
        summary_parts.append(f"{len(protected)} veritabanı eşiklere uygun")
    if breached:
        summary_parts.append(f"{len(breached)} veritabanında yedek eskimiş")
    if unknown:
        summary_parts.append(f"{len(unknown)} veritabanında durum belirlenemedi")

    return SectionResult(
        key="backup",
        title="Yedek durumu",
        status=status,
        summary=", ".join(summary_parts) + "." if summary_parts else "Yedek durumu değerlendirildi.",
        findings=findings,
        data={"instances": instance_rows},
    )


# --------------------------------------------------------------------------------------
# 5. Performans (en pahalı sorgular)
# --------------------------------------------------------------------------------------


def slow_query_link(instance_id: int, entry, start, end) -> str:
    """Bulgudan DPA'ya derin bağlantı — sorgunun kimliği VE penceresiyle birlikte.

    Faz 18 İŞ 1: eskiden bağlantı yalnızca `?tab=queries` idi; DPA varsayılan penceresinde
    (son toplama döngüsü) açılıyor ve raporun bahsettiği sorgu orada bulunamıyordu. Artık
    bağlantı raporun kullandığı pencereyi ve sorgu anahtarını taşıyor, dolayısıyla DPA aynı
    veriyi gösteriyor.
    """
    params = [
        "tab=queries",
        f"qkey={quote(entry.key, safe='')}",
        f"start={quote(start.isoformat(), safe='')}",
        f"end={quote(end.isoformat(), safe='')}",
    ]
    if entry.queryid:
        params.append(f"queryid={quote(str(entry.queryid), safe='')}")
    return f"/instances/{instance_id}?" + "&".join(params)


def explain_feasibility(query: str) -> tuple[bool, str | None]:
    """"EXPLAIN'e bakın" demeden ÖNCE EXPLAIN'in gerçekten alınabildiğini doğrular (Faz 18 İŞ 3).

    `validate_explainable` DPA'nın EXPLAIN ucunun kullandığı aynı kontrol ve canlı bağlantı
    gerektirmiyor — yani rapor, kendi kuralını çiğnemeden (canlı probe yok) yönlendirmenin
    geçerli olup olmadığını bilebiliyor. Geçersizse NEDENİ döndürülüyor ki öneri
    "şu yüzden öneremiyorum" biçiminde yazılabilsin.
    """
    try:
        validate_explainable(query)
    except ValueError as exc:
        return False, str(exc)
    return True, None


def _slow_query_advice(entry, diagnosis, explainable: bool, explain_reason: str | None) -> Advice:
    """Yavaş sorgu bulgusunun önerisi — yönlendirdiği yerin var olduğu doğrulanmış."""
    if not explainable:
        # Boş yönlendirme yapma: neden EXPLAIN'e bakılamayacağını ve bunun yerine ne
        # yapılabileceğini söyle.
        return Advice(
            title="Bu sorgu için plan analizi yapılamıyor — çağrı sıklığını ve uygulama tarafını inceleyin",
            why=(
                f"Sorgu {diagnosis.resource} darboğazı gösteriyor ama EXPLAIN alınamıyor: {explain_reason} "
                "Plan analizi olmadan index önerisi de üretilemez."
            ),
            steps=[
                AdviceStep(
                    "Sorgunun nereden çağrıldığını uygulama tarafında bulun; çağrı sıklığı "
                    "azaltılabiliyorsa en etkili çözüm budur."
                ),
                AdviceStep(
                    "Sorgu bir DML (INSERT/UPDATE/DELETE) ise etkilenen satır sayısını ve "
                    "hedef tablodaki index sayısını gözden geçirin — her index yazma maliyeti ekler.",
                    "SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid)) AS boyut\n"
                    "FROM pg_stat_user_indexes WHERE relname = '<tablo>' ORDER BY idx_scan;",
                ),
            ],
            verification=(
                "-- Bir sonraki raporda bu sorgunun toplam süresi düşmüş olmalı."
            ),
        )

    return Advice(
        title="Bu sorgunun planına ve index önerisine bakın",
        why=(
            f"Sorgu dönemde {entry.total_time_ms:.0f} ms harcadı ve darboğaz {diagnosis.resource} "
            "tarafında görünüyor. Plan, sürenin nereye gittiğini kesin olarak gösterir."
        ),
        steps=[
            AdviceStep(
                "Bulgudaki bağlantıyla DPA'daki Yavaş Sorgular sekmesine gidin — bağlantı aynı "
                "sorguyu ve aynı zaman aralığını açar."
            ),
            AdviceStep("Sorgu satırını açıp EXPLAIN planını alın (sorguyu çalıştırmaz)."),
            AdviceStep("Aynı satırdaki 'Index önerisi' ile aday index'leri değerlendirin."),
        ],
        cautions=[
            "EXPLAIN ANALYZE sorguyu GERÇEKTEN çalıştırır; yalnızca plan için düz EXPLAIN yeterli."
        ],
        verification="-- Değişiklikten sonra bir sonraki raporda bu sorgunun süresi düşmüş olmalı.",
    )


def _slow_query_finding(ctx, instance, entry, diagnosis, change: str, change_pct, mode: str):
    """Yavaş sorgu bulgusu — yapılandırılmış metin ve doğrulanmış derin bağlantıyla."""
    what = "Yeni pahalı sorgu" if change == "new" else "Pahalı sorgu kötüleşti"
    explainable, explain_reason = explain_feasibility(entry.query)

    # Faz 18 İŞ 3: sınırlılıklar bulgu metnine gömülmüyor, ayrı ve kısa bir nota taşınıyor.
    notes: list[str] = []
    if diagnosis.confidence != "observed":
        notes.append(f"Darboğaz sınıfı kesin değil: {diagnosis.reason}")
    if mode == "snapshot":
        notes.append(
            "Pencerede tek toplama döngüsü var; değerler dönem farkı değil kümülatif toplam."
        )
    if not explainable:
        notes.append(f"Bu sorgu için EXPLAIN alınamıyor: {explain_reason}")

    # Faz 18 İŞ 4: "ne oldu" cümlede, "ne kadar / neye göre" etiketli satırlarda.
    facts = [
        fact("Toplam süre", f"{entry.total_time_ms:,.0f} ms".replace(",", "."), "bad"),
        fact("Çağrı", f"{entry.calls:,}".replace(",", ".")),
        fact("Ortalama", f"{entry.mean_time_ms:,.1f} ms".replace(",", "."), "bad"),
        fact("Darboğaz", diagnosis.resource),
    ]
    if change_pct is not None:
        facts.append(
            fact(
                "Önceki döneme göre",
                f"%{change_pct:+.0f}",
                "bad" if change_pct > 0 else "good",
            )
        )
    else:
        facts.append(fact("Önceki dönem", "görülmemişti"))

    return FindingDraft(
        section="performance",
        severity="warning" if change == "worse" else "info",
        title=f"{instance.name}: {what.lower()} — {diagnosis.resource} darboğazı",
        detail=f"{what}. {_short_query(entry.query)}",
        facts=facts,
        note=" ".join(notes) or None,
        evidence={
            "metric": "pg_stat_statements.total_exec_time (dönem farkı)",
            "value": entry.total_time_ms,
            "mean_time_ms": entry.mean_time_ms,
            "calls": entry.calls,
            "change_pct": change_pct,
            "resource": diagnosis.resource,
            "resource_confidence": diagnosis.confidence,
            "queryid": entry.queryid,
            "query_key": entry.key,
            "query": entry.query,
            "selection_mode": mode,
            "explainable": explainable,
            "measured_at": ctx.period_end.isoformat(),
        },
        # Kimlik olarak `key` kullanılıyor: queryid NULL gelebiliyor ve eskiden bu, farklı
        # sorguların AYNI fingerprint'e düşüp birbirini elemesine yol açıyordu.
        fingerprint_parts=("slow_query", str(instance.id), entry.key),
        recommendation=(
            "Bu sorgunun planına ve index önerisine bakın."
            if explainable
            else f"Bu sorgu için plan analizi yapılamıyor ({explain_reason}); uygulama tarafını inceleyin."
        ),
        advice=_slow_query_advice(entry, diagnosis, explainable, explain_reason),
        related_object_type="instance",
        related_object_id=instance.id,
        link_hint=slow_query_link(instance.id, entry, ctx.period_start, ctx.period_end),
        environment=_environment_of(instance),
    )


@register_section
async def performance_section(ctx: ReportContext) -> SectionResult:
    """Performans — en pahalı sorgular, düne göre değişim, darboğaz sınıfı.

    Sorgu seçimi DPA ile AYNI servisten (`services/slow_query_selection.py`) geliyor. Bu,
    raporun bahsettiği her sorgunun DPA'da da bulunabilmesini yapısal olarak garanti ediyor —
    Faz 18 İŞ 1'de düzeltilen tutarsızlığın kaynağı iki ayrı seçim mantığıydı.
    """
    window = ctx.period_end - ctx.period_start
    previous_start, previous_end = ctx.period_start - window, ctx.period_start

    findings: list[FindingDraft] = []
    all_rows: list[dict] = []
    instances_without_data: list[str] = []
    filtered_system = 0
    filtered_insignificant = 0

    # Gürültü eşikleri DPA ile ORTAK ayardan (Faz 18 İŞ 2).
    noise = await get_noise_settings(ctx.session)

    for instance in ctx.instances:
        selection = await select_slow_queries(
            ctx.session,
            instance.id,
            start=ctx.period_start,
            end=ctx.period_end,
            sort="total",
            limit=TOP_QUERIES,
            include_system=noise["show_system_queries"],
            min_total_ms=noise["list_min_total_ms"],
            min_calls=noise["list_min_calls"],
        )
        filtered_system += selection.filtered_system
        filtered_insignificant += selection.filtered_insignificant
        if not selection.entries:
            instances_without_data.append(instance.name)
            continue

        previous = await select_slow_queries(
            ctx.session,
            instance.id,
            start=previous_start,
            end=previous_end,
            limit=10_000,
            min_total_ms=0.0,
            min_calls=0,
            include_system=True,
        )
        previous_by_key = {e.key: e for e in previous.entries}

        for entry in selection.entries:
            before = previous_by_key.get(entry.key)
            if before is None:
                change, change_pct = "new", None
            else:
                base = before.total_time_ms or 1
                change_pct = round((entry.total_time_ms - base) / base * 100, 1)
                change = "worse" if change_pct >= 25 else "better" if change_pct <= -25 else "stable"

            diagnosis = diagnose_query(entry.sample)
            all_rows.append(
                {
                    "instance_id": instance.id,
                    "instance": instance.name,
                    "key": entry.key,
                    "queryid": entry.queryid,
                    "query": entry.query[:500],
                    "total_time_ms": entry.total_time_ms,
                    "calls": entry.calls,
                    "mean_time_ms": entry.mean_time_ms,
                    "change": change,
                    "change_pct": change_pct,
                    "resource": diagnosis.resource,
                    "resource_reason": diagnosis.reason,
                    "resource_confidence": diagnosis.confidence,
                    "mode": selection.mode,
                }
            )

            if not _is_finding_worthy(entry, change, noise):
                continue

            findings.append(
                _slow_query_finding(ctx, instance, entry, diagnosis, change, change_pct, selection.mode)
            )

    if not all_rows:
        return SectionResult(
            key="performance",
            title="Performans",
            status="unknown",
            summary="Bu dönem için değerlendirilebilir yavaş sorgu yok.",
            data={
                "instances_without_data": instances_without_data,
                "filtered_system": filtered_system,
                "filtered_insignificant": filtered_insignificant,
            },
            unknown_reason=(
                "Dönem içinde eşikleri geçen bir sorgu kaydedilmemiş. pg_stat_statements ön "
                "koşulları eksik olabilir — Ön koşullar bölümüne bakın."
                if not (filtered_system or filtered_insignificant)
                else (
                    f"Kaydedilen sorguların tamamı filtrelendi: {filtered_system} sistem/platform "
                    f"sorgusu, {filtered_insignificant} eşik altı sorgu."
                )
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
            "filtered_system": filtered_system,
            "filtered_insignificant": filtered_insignificant,
        },
    )


def _is_finding_worthy(entry, change: str, noise: dict) -> bool:
    """Bir sorgunun BULGU üretmeye değip değmediği (Faz 18 İŞ 2 — gürültü filtresi).

    Listede görünmek ile bulgu üretmek FARKLI eşikler: liste "en pahalı N"i gösteren bir
    keşif aracı, bulgu ise DBA'dan dikkat isteyen bir talep. Eşikler yönetim ayarından
    geliyor; ortamdan ortama doğru değer değişir (OLTP'de 1 sn ciddi, raporlama
    veritabanında sıradan).
    """
    if change not in ("new", "worse"):
        return False
    if entry.mean_time_ms < SLOW_QUERY_MEAN_MS:
        return False
    # Tek çağrılık bir sorgudan yüzde değişimi anlamsız — trend bulgusu üretme.
    if entry.calls < noise["finding_min_calls"]:
        return False
    if entry.total_time_ms < noise["finding_min_total_ms"]:
        return False
    return True


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

        # Faz 18 İŞ 5 denetimi: temp_bytes KÜMÜLATİF bir sayaç (pg_stat_database.temp_bytes).
        # Eskiden max() alınıyordu; bu yalnızca son değeri verir, yani geçmişte bir kez geçici
        # dosya kullanmış her veritabanı sonsuza kadar bu bulguyu üretirdi. Doğrusu dönem farkı.
        temp_values = [float(s.temp_bytes or 0) for s in samples]
        if len(temp_values) >= 2:
            # Sayaç sıfırlanmışsa (pg_stat_reset) son değeri olduğu gibi al.
            temp_delta = (
                temp_values[-1]
                if temp_values[-1] < temp_values[0]
                else temp_values[-1] - temp_values[0]
            )
        else:
            temp_delta = 0.0

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
                "temp_bytes_in_period": temp_delta,
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
                    detail=f"{instance.name} bağlantı doluluğunda zirve yaptı.",
                    facts=[
                        fact("Zirve doluluk", f"%{peak_util:.0f}", "bad"),
                        fact("Bağlantı", f"{peak_conn}/{max_conn}", "bad"),
                        fact("Zirve saati", peak_conn_at.strftime("%H:%M") if peak_conn_at else "—"),
                    ],
                    note="Doluluk %100'e ulaşırsa yeni bağlantılar reddedilir.",
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
                    advice=Advice(
                        title="Bağlantı havuzunu devreye alın veya havuz boyutunu sınırlayın",
                        why=(
                            "Bağlantı doluluğu %100'e ulaştığında veritabanı yeni bağlantı kabul etmez ve "
                            "uygulama hata vermeye başlar — bu, plansız bir kesinti demektir."
                        ),
                        steps=[
                            AdviceStep(
                                "Bağlantıların hangi durumda olduğunu görün: boşta bekleyenler mi, işlem "
                                "içinde takılanlar mı?",
                                "SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;",
                            ),
                            AdviceStep(
                                "Bağlantıyı hangi uygulamanın tuttuğunu belirleyin.",
                                "SELECT usename, application_name, client_addr, count(*)\n"
                                "FROM pg_stat_activity GROUP BY 1, 2, 3 ORDER BY 4 DESC;",
                            ),
                            AdviceStep(
                                "Uygulama tarafındaki havuz boyutunu sınırlayın ya da PgBouncer'ı transaction "
                                "modunda devreye alın. max_connections'ı büyütmek sorunu bellek sorununa çevirir.",
                            ),
                            AdviceStep(
                                "İşlem içinde takılı kalan oturumlar için zaman aşımı koyun.",
                                "ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';\n"
                                "SELECT pg_reload_conf();",
                            ),
                        ],
                        cautions=[
                            "max_connections değişikliği reload ile devreye GİRMEZ; PostgreSQL yeniden "
                            "başlatılmalıdır (kesinti).",
                            "Her bağlantı work_mem kadar bellek isteyebilir — artırmadan önce toplam belleği "
                            "hesaplayın.",
                        ],
                        estimated_duration="Havuz ayarı: dakikalar. PgBouncer kurulumu: 1-2 saat.",
                        rollback="idle_in_transaction_session_timeout = 0 ile zaman aşımı kaldırılabilir.",
                        verification=(
                            "SELECT (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS azami,\n"
                            "       count(*) AS toplam,\n"
                            "       round(100.0 * count(*) / (SELECT setting::int FROM pg_settings "
                            "WHERE name = 'max_connections'), 1) AS doluluk_yuzde\n"
                            "FROM pg_stat_activity;"
                        ),
                    ),
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
                    detail=f"{instance.name} üzerinde veri diskten okunuyor.",
                    facts=[
                        fact("Dönem ortalaması", f"%{cache_avg}", "bad"),
                        fact("En düşük", f"%{cache_min}", "bad"),
                        fact("Eşik", f"%{CACHE_HIT_WARN:.0f}"),
                    ],
                    note="shared_buffers yetersiz ya da sorgular gereksiz çok veri tarıyor olabilir.",
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
                    advice=Advice(
                        title="Bellek ayarlarını ve en çok disk okuyan sorguları gözden geçirin",
                        why=(
                            "Veri diskten okunduğunda sorgular bellekten okunduğuna göre kat kat yavaşlar; "
                            "yoğun saatlerde uygulama yanıt süreleri belirgin şekilde uzar."
                        ),
                        steps=[
                            AdviceStep(
                                "Mevcut bellek ayarlarını görün.",
                                "SELECT name, setting, unit FROM pg_settings\n"
                                "WHERE name IN ('shared_buffers', 'effective_cache_size', 'work_mem');",
                            ),
                            AdviceStep(
                                "En çok disk okuyan sorguları bulun — sorun ayar değil, eksik index olabilir.",
                                "SELECT queryid, calls, shared_blks_read, shared_blks_hit,\n"
                                "       round(100.0 * shared_blks_read / "
                                "NULLIF(shared_blks_read + shared_blks_hit, 0), 1) AS disk_yuzde\n"
                                "FROM pg_stat_statements\n"
                                "ORDER BY shared_blks_read DESC LIMIT 10;",
                            ),
                            AdviceStep(
                                "Gerekiyorsa shared_buffers'ı toplam belleğin ~%25'ine ayarlayın.",
                                "ALTER SYSTEM SET shared_buffers = '4GB';\n"
                                "-- Ardından PostgreSQL'i YENİDEN BAŞLATIN.",
                            ),
                        ],
                        cautions=[
                            "shared_buffers değişikliği yeniden başlatma gerektirir (kesinti).",
                            "Çok büyük shared_buffers işletim sistemi önbelleğini küçültüp ters etki yapabilir.",
                        ],
                        estimated_duration="Ayar: dakikalar + yeniden başlatma penceresi.",
                        rollback="ALTER SYSTEM RESET shared_buffers; ardından yeniden başlatma.",
                        verification=(
                            "SELECT round(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2) AS cache_hit_yuzde\n"
                            "FROM pg_stat_database WHERE datname = current_database();"
                        ),
                    ),
                    related_object_type="instance",
                    related_object_id=instance.id,
                    environment=_environment_of(instance),
                )
            )

        # Eşik: dönemde en az 1 MB geçici dosya. Birkaç kilobayt her veritabanında olur.
        if temp_delta >= TEMP_BYTES_WARN:
            findings.append(
                FindingDraft(
                    section="resources",
                    severity="info",
                    title=f"{instance.name}: geçici dosya kullanımı var",
                    detail=f"{instance.name} üzerinde sıralama/hash işlemleri diske taşıyor.",
                    facts=[
                        fact("Dönemde geçici dosya", _format_bytes(temp_delta), "bad"),
                        fact("Eşik", _format_bytes(TEMP_BYTES_WARN)),
                    ],
                    note="work_mem sıralama/hash için yetersiz kalıyor olabilir.",
                    evidence={
                        "metric": "temp_bytes (dönem farkı)",
                        "value": temp_delta,
                        "threshold": TEMP_BYTES_WARN,
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
            # Dürüstlük kuralı (Faz 17 İŞ 6): ölçmediğimiz şey "sorunsuz" değil "bilinmiyor".
            # Bu, veri yapısında açık bir alan olarak duruyor; sadece bir dipnot değil.
            "os_metrics": {
                "status": "unknown",
                "reason": (
                    "Sunucu seviyesi CPU/RAM/disk metrikleri dbace tarafından toplanmıyor. Bu "
                    "bölümdeki 'sorun yok' değerlendirmesi YALNIZCA veritabanı içi göstergeler "
                    "içindir; işletim sistemi tarafında sorun olup olmadığı bilinmiyor."
                ),
            },
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
                "Şema verisi günde bir kez toplanıyor (günlük rollup işi). "
                + needs_more_days(0, SCHEMA_MIN_DAYS, "Tablo/index büyümesi")
            ),
        )

    # Faz 18 İŞ 5 denetimi: iki farklı bulgu tipinin VERİ İHTİYACI farklı.
    #   * Büyüme trendi  → en az iki günlük fotoğraf (fark alınabilmesi için).
    #   * Kullanılmayan index → tek fotoğraf yeter (idx_scan = 0 bugünün gerçeği).
    # Eskiden ikisi de büyüme eşiğinin arkasındaydı; sonuç olarak ilk iki gün boyunca
    # kullanılmayan indexler HİÇ raporlanmıyor, bölüm tamamen "bilinmiyor" dönüyordu.
    distinct_days = len({r.day for r in rows})
    growth_ready = distinct_days >= SCHEMA_MIN_DAYS

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

        if growth_ready and growth > 0 and len(group) >= 2:
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

    # Faz 18 İŞ 1: bulgu instance BAŞINA üretiliyor. Eskiden kapsam seviyesinde tek bir bulgu
    # vardı ve hiçbir nesneye bağlı olmadığı için hiçbir sayfaya bağlantı veremiyordu —
    # "Şema sekmesinden DROP komutlarını alın" diyordu ama hangi instance'ın sekmesi belirsizdi.
    by_instance: dict[int, list[dict]] = {}
    for index in unused_indexes:
        by_instance.setdefault(index["instance_id"], []).append(index)

    for instance_id, indexes in by_instance.items():
        instance = ctx.instance_by_id(instance_id)
        total_bytes = sum(i["size_bytes"] for i in indexes)
        findings.append(
            FindingDraft(
                section="schema",
                severity="info",
                title=(
                    f"{instance.name if instance else instance_id}: {len(indexes)} kullanılmayan index "
                    f"({_format_bytes(total_bytes)})"
                ),
                detail=(
                    "Hiç taranmamış (idx_scan = 0) indexler hem disk kaplıyor hem de her yazma işlemine "
                    "maliyet ekliyor: "
                    + ", ".join(f"{i['object']} ({_format_bytes(i['size_bytes'])})" for i in indexes[:5])
                ),
                evidence={
                    "metric": "pg_stat_user_indexes.idx_scan",
                    "value": len(indexes),
                    "total_bytes": total_bytes,
                    "measured_at": ctx.period_end.isoformat(),
                },
                fingerprint_parts=("unused_indexes", str(instance_id)),
                recommendation=(
                    "Şema sekmesindeki DROP komutlarını kullanın; indexin gerçekten gereksiz olduğunu "
                    "(ör. sadece ayda bir çalışan bir rapor kullanmıyor mu) önce doğrulayın."
                ),
                related_object_type="instance",
                related_object_id=instance_id,
                environment=_environment_of(instance) if instance else "prod",
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
    growth_note = (
        None
        if growth_ready
        else needs_more_days(distinct_days, SCHEMA_MIN_DAYS, "Tablo/index büyümesi")
    )
    summary = (
        f"{len(growing)} büyüyen nesne, {len(unused_indexes)} kullanılmayan index "
        f"({_format_bytes(unused_total)})."
        if growth_ready
        else (
            f"{len(unused_indexes)} kullanılmayan index ({_format_bytes(unused_total)}). "
            "Büyüme trendi için henüz yeterli gün yok."
        )
    )
    return SectionResult(
        key="schema",
        title="Şema sağlığı",
        status=status,
        summary=summary,
        findings=findings,
        data={
            "growing_objects": growing[:20],
            "unused_indexes": unused_indexes[:20],
            "days_observed": distinct_days,
            "days_required": SCHEMA_MIN_DAYS,
            "growth_ready": growth_ready,
            "growth_note": growth_note,
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


def _deadlock_facts(events) -> list[dict]:
    """Deadlock bulgusunun sayısal özeti + taraf sorguları.

    Sorgular kısaltılıyor: bulgu kartı bir teşhis özeti, tam sorgu metni değil. Tam metin
    Bloklama sekmesindeki geçmiş listesinde duruyor.
    """
    latest = events[0]
    facts = [
        {"label": "Deadlock sayısı", "value": str(len(events)), "tone": "bad"},
        {
            "label": "Farklı desen",
            "value": str(len({e.fingerprint for e in events})),
            "tone": "neutral",
        },
    ]
    if latest.victim_query:
        facts.append(
            {"label": "İptal edilen (kurban)", "value": _short_query(latest.victim_query), "tone": "bad"}
        )
    if latest.winner_query:
        # Kazanan "iyi taraf" değil: döngüyü oluşturan kilit sırası genelde onundur.
        facts.append(
            {"label": "Devam eden (kazanan)", "value": _short_query(latest.winner_query), "tone": "neutral"}
        )
    return facts


def _short_query(text: str, limit: int = 120) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


@register_section
async def blocking_section(ctx: ReportContext) -> SectionResult:
    """Bloklama ve deadlock — dönem içinde kim kimi bekletti (Faz 26 İŞ 3).

    Canlı ekran "şu anda" sorusunu cevaplıyor; rapor "dün gece" sorusunu. En kötü bloklama
    olayları kimsenin ekrana bakmadığı saatlerde yaşanır ve sabah geriye kalan tek şey "gece
    sistem yavaştı" cümlesidir.
    """
    instance_ids = ctx.instance_ids()
    if not instance_ids:
        return SectionResult(
            key="blocking", title="Bloklama ve deadlock", status="unknown",
            summary="Kapsamda instance yok.",
            unknown_reason="Kapsama bağlı instance bulunamadı.",
        )

    episodes = await recent_episodes(ctx.session, instance_ids, ctx.period_start, ctx.period_end)
    deadlocks = (
        await ctx.session.execute(
            select(DeadlockEvent)
            .where(
                DeadlockEvent.instance_id.in_(instance_ids),
                DeadlockEvent.detected_at >= ctx.period_start,
                DeadlockEvent.detected_at <= ctx.period_end,
            )
            .order_by(DeadlockEvent.detected_at.desc())
            .limit(20)
        )
    ).scalars().all()

    if not episodes and not deadlocks:
        # "Bloklama olmadı" ile "bloklama ölçülmedi" farklı şeyler. Örnekleyici kapalıysa
        # ikincisi geçerli ve bunu "sorunsuz" diye raporlamak yanıltıcı olurdu.
        if not settings.wait_sampling_enabled:
            return SectionResult(
                key="blocking", title="Bloklama ve deadlock", status="unknown",
                summary="Bloklama izleme kapalı.",
                unknown_reason=(
                    "Bekleme örnekleyicisi kapalı (WAIT_SAMPLING_ENABLED=false). Bloklama "
                    "olayları örnekleme sırasında tespit ediliyor; kapalıyken hiçbir olay "
                    "kaydedilmez. Bu, 'bloklama olmadı' anlamına GELMEZ."
                ),
            )
        return SectionResult(
            key="blocking", title="Bloklama ve deadlock", status="ok",
            summary="Dönem içinde kayda değer bloklama ya da deadlock görülmedi.",
        )

    findings: list[FindingDraft] = []
    episode_rows: list[dict] = []
    for episode in episodes:
        episode_rows.append(
            {
                "instance_id": episode.instance_id,
                "started_at": as_utc(episode.started_at).isoformat(),
                "ended_at": as_utc(episode.ended_at).isoformat() if episode.ended_at else None,
                "duration_seconds": round(episode.duration_seconds, 1),
                "root_pid": episode.root_pid,
                "root_was_idle": episode.root_was_idle,
                "max_blocked_sessions": episode.max_blocked_sessions,
                "max_chain_depth": episode.max_chain_depth,
                "lock_object": episode.lock_object,
            }
        )

    worst = max(episodes, key=lambda e: e.max_blocked_sessions, default=None)
    if worst is not None and worst.max_blocked_sessions >= BLOCKING_FINDING_MIN_SESSIONS:
        # SESSİZ BLOK AYRIMI raporun en değerli kısmı: kök engelleyici sorgu çalıştırmıyorsa
        # sorun veritabanında değil uygulamadadır ve DBA'nın yapabileceği kalıcı bir şey yok.
        if worst.root_was_idle:
            detail = (
                f"Zincirin başındaki oturum (pid {worst.root_pid}) hiçbir sorgu çalıştırmıyordu "
                f"— açık transaction'ıyla en fazla {worst.max_blocked_sessions} oturumu "
                f"{worst.duration_seconds:.0f} saniye bekletti. Bu bir veritabanı sorunu "
                "değildir: uygulama transaction'ı açmış ve kapatmamıştır."
            )
            recommendation = (
                "Uygulamada transaction kapsamını daraltın ve sunucu tarafında "
                "idle_in_transaction_session_timeout ayarlayın."
            )
        else:
            detail = (
                f"Zincirin başındaki oturum (pid {worst.root_pid}) en fazla "
                f"{worst.max_blocked_sessions} oturumu {worst.duration_seconds:.0f} saniye "
                f"bekletti (zincir derinliği {worst.max_chain_depth})."
            )
            recommendation = (
                "Bloklayan sorguyu inceleyin; kilit kapsamını daraltın ya da toplu işlemleri "
                "parçalara bölün."
            )
        findings.append(
            FindingDraft(
                section="blocking",
                severity="warning" if worst.max_blocked_sessions < BLOCKING_CRITICAL_SESSIONS else "critical",
                title=(
                    f"Bloklama: {worst.max_blocked_sessions} oturum "
                    f"{worst.duration_seconds:.0f} sn bekledi"
                ),
                detail=detail,
                evidence={
                    "metric": "blocked_sessions",
                    "value": worst.max_blocked_sessions,
                    "threshold": BLOCKING_FINDING_MIN_SESSIONS,
                    "measured_at": as_utc(worst.started_at).isoformat(),
                },
                facts=[
                    {"label": "Bekleyen oturum (zirve)", "value": str(worst.max_blocked_sessions), "tone": "bad"},
                    {"label": "Süre", "value": f"{worst.duration_seconds:.0f} sn", "tone": "bad"},
                    {"label": "Zincir derinliği", "value": str(worst.max_chain_depth), "tone": "neutral"},
                    {
                        "label": "Kök engelleyici",
                        "value": "sorgu çalıştırmıyordu" if worst.root_was_idle else "sorgu çalıştırıyordu",
                        "tone": "bad" if worst.root_was_idle else "neutral",
                    },
                ],
                recommendation=recommendation,
                fingerprint_parts=("blocking", str(worst.instance_id), str(worst.root_pid)),
                related_object_type="instance",
                related_object_id=worst.instance_id,
            )
        )

    # KURBAN VE KAZANAN SORGULARI (Faz 26 İŞ 3c). Öncesinde yalnızca pid'ler vardı ve pid
    # olaydan sonra hiçbir şey ifade etmiyor — süreç çoktan kapanmış oluyor. Teşhis için
    # gereken şey SORGULAR: kurbanın "suçu" genelde yoktur, döngüyü oluşturan kilit sırası
    # kazananındır ve düzeltme orada yapılır.
    deadlock_rows = [
        {
            "instance_id": event.instance_id,
            "detected_at": as_utc(event.detected_at).isoformat(),
            "victim_pid": event.victim_pid,
            "victim_query": event.victim_query,
            "winner_pid": event.winner_pid,
            "winner_query": event.winner_query,
            "participants": event.participants,
            "source": event.source,
        }
        for event in deadlocks
    ]
    if deadlocks:
        findings.append(
            FindingDraft(
                section="blocking",
                severity="warning",
                title=f"Deadlock: dönem içinde {len(deadlocks)} olay",
                detail=(
                    f"Dönem içinde {len(deadlocks)} deadlock tespit edildi. Veritabanı döngüyü "
                    "kırıp bir tarafı iptal etti; iptal edilen tarafta uygulama hata aldı. "
                    "Deadlock kendiliğinden 'çözülmüş' sayılmaz — tekrar ediyorsa kilit sırası "
                    "uygulamada tutarsızdır."
                ),
                evidence={
                    "metric": "deadlock_count",
                    "value": len(deadlocks),
                    "threshold": 1,
                    "measured_at": as_utc(deadlocks[0].detected_at).isoformat(),
                },
                facts=_deadlock_facts(deadlocks),
                recommendation=(
                    "Taraf sorguları aynı tabloları AYNI SIRADA kilitleyecek şekilde "
                    "düzenlenmeli; deadlock'ın çözümü yeniden deneme değil, sıra tutarlılığıdır."
                ),
                fingerprint_parts=("deadlock", str(deadlocks[0].instance_id)),
                related_object_type="instance",
                related_object_id=deadlocks[0].instance_id,
            )
        )

    status = "ok"
    if findings:
        status = "critical" if any(f.severity == "critical" for f in findings) else "warning"

    return SectionResult(
        key="blocking",
        title="Bloklama ve deadlock",
        status=status,
        summary=(
            f"{len(episodes)} bloklama olayı, {len(deadlocks)} deadlock."
            if episodes or deadlocks
            else "Kayda değer bloklama görülmedi."
        ),
        findings=findings,
        data={"episodes": episode_rows, "deadlocks": deadlock_rows},
    )


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


def _parameter_link(instance) -> str | None:
    """Parametre bulgusunun hedefi.

    Faz 18 İŞ 1: parametre denetimi arayüzü GRUP sayfasında (`/groups/{id}?tab=parameters`);
    instance detayının "tuning" sekmesinde parametre yok, ön koşullar ve tanı var. Bölüm→sekme
    varsayılan eşlemesi bu yüzden yanlış sayfaya götürüyordu.

    Gruba bağlı olmayan bir instance için parametre denetimi sayfası YOK; böyle bir durumda
    bağlantı üretmiyoruz (var olmayan bir sayfaya söz vermektense bağlantısız bırakmak doğru).
    """
    group_id = getattr(instance, "group_id", None) if instance else None
    return f"/groups/{group_id}?tab=parameters" if group_id else None


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
                "Parametreler günde bir kez (günlük rollup işi) kaydediliyor. "
                + needs_more_days(0, 1, "Parametre denetimi")
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
                        link_hint=_parameter_link(instance),
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
                        link_hint=_parameter_link(instance),
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
# 8b. Düğümler arası yapılandırma sapması (Faz 28 İŞ 4)
# --------------------------------------------------------------------------------------


@register_section
async def config_drift_section(ctx: ReportContext) -> SectionResult:
    """Düğümler arası yapılandırma sapması.

    Parametre denetimi bölümü ZAMAN eksenli: "dünden beri ne değişti". Bu bölüm DÜĞÜMLER
    ARASI: primary ile replika aynı mı. Cluster'da asıl arıza sebebi budur ve sapma normal
    çalışmada hiçbir belirti vermez — tam olarak failover anında, yani en kötü anda ortaya
    çıkar.

    Rapor canlı probe yapmıyor: karşılaştırma günlük yapılandırma fotoğraflarından
    üretiliyor (services/config_comparison.py).
    """
    grouped: dict[int, list[Instance]] = {}
    for instance in ctx.instances:
        if instance.group_id:
            grouped.setdefault(instance.group_id, []).append(instance)
    multi_node = {gid: rows for gid, rows in grouped.items() if len(rows) >= 2}

    if not multi_node:
        return SectionResult(
            key="config_drift", title="Yapılandırma karşılaştırması", status="unknown",
            summary="Karşılaştırılacak çok düğümlü grup yok.",
            unknown_reason=(
                "Yapılandırma karşılaştırması en az iki düğümlü gruplar için yapılıyor; "
                "bu kapsamda öyle bir grup yok."
            ),
        )

    findings: list[FindingDraft] = []
    group_rows: list[dict] = []
    measured_groups = 0

    for group_id, instances in sorted(multi_node.items()):
        group = await ctx.session.get(DatabaseGroup, group_id)
        if group is None:
            continue
        comparison = await compare_group_from_snapshots(ctx.session, group, instances)
        comparison["group_id"] = group_id
        comparison["group_name"] = group.name
        group_rows.append(comparison)

        readable = [n for n in comparison["nodes"] if n not in (comparison.get("errors") or {})]
        if len(readable) < 2:
            # İki düğümün yapılandırması okunamadıysa "sapma yok" DEMİYORUZ; ölçüm eksikliğini
            # uyum gibi sunmak, tam da görülmesi gereken sapmayı gizlerdi.
            continue
        measured_groups += 1

        for row in comparison["rows"]:
            if not row["diverged"] or row["severity"] not in ("critical", "warning"):
                continue
            value_text = ", ".join(
                f"{node}: {value if value is not None else 'okunamadı'}"
                for node, value in row["values"].items()
            )
            findings.append(
                FindingDraft(
                    section="config_drift",
                    severity=row["severity"],
                    title=f"{group.name}: düğümler arası {row['name']} farkı",
                    detail=(
                        f"`{row['name']}` düğümler arasında farklı: {value_text}. "
                        + row["consequence"]
                    ),
                    evidence={
                        "metric": f"config_drift.{row['name']}",
                        "value": value_text[:200],
                        "threshold": "tüm düğümlerde aynı",
                        "measured_at": ctx.period_end.isoformat(),
                    },
                    facts=[
                        fact("Sınıf", row["drift_class_label"],
                             "bad" if row["severity"] == "critical" else "neutral"),
                        *[
                            fact(node, str(value) if value is not None else "okunamadı")
                            for node, value in list(row["values"].items())[:4]
                        ],
                    ],
                    note=(
                        "Okunamayan düğümler: " + ", ".join(row["missing_nodes"])
                        if row["missing_nodes"]
                        else None
                    ),
                    recommendation=(
                        f"`{row['name']}` değerini tüm düğümlerde eşitleyin."
                    ),
                    advice=_config_drift_advice(group, row),
                    fingerprint_parts=("config_drift", str(group_id), row["name"]),
                    related_object_type="group",
                    related_object_id=group_id,
                )
            )

    if not measured_groups:
        return SectionResult(
            key="config_drift", title="Yapılandırma karşılaştırması", status="unknown",
            summary="Yapılandırma fotoğrafı yeterli değil.",
            data={"groups": group_rows},
            unknown_reason=(
                "Karşılaştırma için en az iki düğümün yapılandırma fotoğrafı gerekiyor; "
                "fotoğraflar günde bir kez alınıyor. " + needs_more_days(0, 1, "Karşılaştırma")
            ),
        )

    total_diverged = sum(g["diverged_count"] for g in group_rows)
    status = _worst_status([f.severity for f in findings]) if findings else "ok"
    summary = (
        f"{measured_groups} grupta {total_diverged} parametre farkı bulundu."
        if total_diverged
        else f"{measured_groups} grubun düğümleri arasında kayda değer fark yok."
    )
    return SectionResult(
        key="config_drift", title="Yapılandırma karşılaştırması", status=status,
        summary=summary, findings=findings, data={"groups": group_rows},
    )


def _config_drift_advice(group, row: dict) -> Advice:
    """Sapma önerisi — iş etkisi FAILOVER RİSKİ olarak anlatılıyor.

    "Değerleri eşitleyin" demek yetmiyor: sapmanın neden şimdi önemli olduğu, ancak
    failover anında ne olacağı söylenerek anlaşılıyor.
    """
    must_match = row["drift_class"] == "must_match"
    return Advice(
        title=f"`{row['name']}` değerini tüm düğümlerde eşitleyin",
        why=(
            f"{group.name} grubunun düğümleri `{row['name']}` için farklı değerler taşıyor. "
            + row["consequence"]
            + " Sapma normal çalışmada hiçbir belirti vermez; tam olarak failover anında "
            "ortaya çıkar — yani en kötü anda ve en az hazırlıklı olunan anda."
        ),
        steps=[
            AdviceStep(
                "Hangi değerin doğru olduğuna karar verin: kural, en yüksek yükü taşıyan "
                "düğümün değeri değil, tüm düğümlerin kaldırabileceği değerdir."
            ),
            AdviceStep(
                "Değeri sapma gösteren düğümlerde ayarlayın.",
                "ALTER SYSTEM SET <parametre> = '<deger>';\nSELECT pg_reload_conf();"
                if group.engine == "postgresql"
                else "EXEC sp_configure '<parametre>', <deger>;\nRECONFIGURE;",
            ),
            AdviceStep(
                "Yeniden başlatma gerektiren parametrelerde (shared_buffers, "
                "max_connections, max_worker_processes) önce REPLİKALARI, en son "
                "switchover ile primary'yi yeniden başlatın."
                if group.engine == "postgresql"
                else "Bellek ve MAXDOP ayarları anında geçerli olur; trace flag'ler için "
                "başlangıç parametrelerini de güncelleyin, yoksa yeniden başlatmada kaybolur."
            ),
            AdviceStep(
                "Yapılandırma yönetimi (Ansible/Puppet) kullanıyorsanız değeri ORADA "
                "düzeltin; elle yapılan değişiklik bir sonraki dağıtımda geri alınır."
            ),
        ],
        cautions=[
            (
                "Bu parametre PostgreSQL tarafından zorunlu tutuluyor: standby'daki değer "
                "primary'dekinden küçükse kurtarma DURUR. Önce standby'ları yükseltin, "
                "sonra primary'yi."
                if must_match and group.engine == "postgresql"
                else "Değişiklik öncesi mevcut değerleri kaydedin; geri alma bunu gerektirir."
            ),
            "Tüm düğümleri aynı anda yeniden başlatmayın — bakım penceresi tanımlayıp "
            "sırayla ilerleyin.",
        ],
        estimated_duration="Yapılandırma dakikalar; yeniden başlatma gerekiyorsa düğüm başına kısa kesinti.",
        rollback=(
            "ALTER SYSTEM RESET <parametre>; SELECT pg_reload_conf();"
            if group.engine == "postgresql"
            else "EXEC sp_configure '<parametre>', <eski_deger>; RECONFIGURE;"
        ),
        verification=(
            "SELECT name, setting FROM pg_settings WHERE name = '<parametre>';"
            if group.engine == "postgresql"
            else "SELECT name, value_in_use FROM sys.configurations WHERE name = '<parametre>';"
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
                "Ön koşullar günde bir kez (günlük rollup işi) kaydediliyor. "
                + needs_more_days(0, 1, "Ön koşul denetimi")
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
    # BASTIRILMIŞ BULGULAR ÖZETE GİRMEZ (Faz 28 İŞ 2). Bir düğüm düştüğünde özet "40 kritik"
    # derse "kritik" kelimesi anlamını yitirir; asıl söylenmesi gereken tek şey düğümün
    # erişilemez olduğu. Bastırılanlar kaybolmuyor, ayrı sayılıyor.
    plan = ctx.suppression
    all_drafts = _all_drafts(results)
    if plan is not None:
        drafts = [
            d
            for d in all_drafts
            if not plan.is_suppressed(ctx.draft_fingerprints.get(id(d), ""))
        ]
    else:
        drafts = all_drafts

    def _is_root(draft) -> bool:
        return plan is not None and plan.is_root(ctx.draft_fingerprints.get(id(draft), ""))

    ranked = sorted(
        drafts,
        key=lambda d: (
            # Kök sebep en üstte: 39 bulguyu doğuran şey, özetin ilk maddesi olmalı.
            1 if _is_root(d) else 0,
            _SEVERITY_RANK.get(d.severity, 0),
            _ENV_RANK.get(d.environment, 1.0),
        ),
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
            "is_root_cause": _is_root(d),
        }
        for d in ranked[:5]
        if d.severity in ("critical", "warning")
    ]

    unknown_sections = [r.title for r in results if r.status == "unknown"]
    critical = sum(1 for d in drafts if d.severity == "critical")
    warning = sum(1 for d in drafts if d.severity == "warning")
    suppressed_rows = plan.summary_rows() if plan is not None else []
    suppressed_total = sum(r["suppressed_count"] for r in suppressed_rows)
    suppressed_note = (
        " Ayrıca kök sebep nedeniyle "
        f"{suppressed_total} kontrol yapılamadı; bulguları kök sebebin altında listelendi."
        if suppressed_total
        else ""
    )

    if critical:
        status, summary = "critical", f"{critical} kritik, {warning} uyarı bulgusu var.{suppressed_note}"
    elif warning:
        status, summary = "warning", f"Kritik bulgu yok; {warning} uyarı var.{suppressed_note}"
    elif unknown_sections:
        status, summary = "info", (
            "Bulgu yok, ancak bazı bölümler yeterli veri olmadığı için değerlendirilemedi."
            + suppressed_note
        )
    else:
        status, summary = "ok", (
            "Bu dönemde dikkat gerektiren bir bulgu tespit edilmedi." + suppressed_note
        )

    return SectionResult(
        key="executive_summary",
        title="Yönetici özeti",
        status=status,
        summary=summary,
        data={
            "highlights": highlights,
            "critical_count": critical,
            "warning_count": warning,
            # "Kök sebep nedeniyle N kontrol yapılamadı" satırları — arayüz bunları
            # açılabilir bir blok olarak gösteriyor.
            "suppression": {"roots": suppressed_rows, "suppressed_total": suppressed_total},
            # Dürüstlük: değerlendirilemeyen bölümler özet seviyesinde de görünür, "sorunsuz"
            # izlenimi yaratılmaz (Faz 17 İŞ 6).
            "unknown_sections": unknown_sections,
            "period": {"start": ctx.period_start.isoformat(), "end": ctx.period_end.isoformat()},
            "scope": {"type": ctx.scope.scope_type, "id": ctx.scope.scope_id, "label": ctx.scope.label},
            "instance_count": len(ctx.instances),
        },
    )


@register_summary_section
async def work_done_section(ctx: ReportContext, results: list[SectionResult]) -> SectionResult:
    """Bu dönemde yapılanlar (Faz 28 İŞ 5).

    Müşteriye DBA ekibinin çalıştığını gösteren şey bu. Rapor bugüne kadar yalnızca "şu anda
    ne sorun var" diyordu; emeğin görünmemesi, hizmetin değerinin de görünmemesi demek.

    Özet bölümü olarak kaydedildi çünkü raporda ÖNDE durması gerekiyor: "ne yapıldı" sorusu,
    "ne kaldı" sorusundan önce cevaplanmalı.

    Bulgu ÜRETMİYOR: yapılan iş bir sorun değil. Bulgu üretseydi "10 konu kapatıldı" satırı
    kritik sayacına girerdi.
    """
    work = await collect_work_done(
        ctx.session, ctx.instances, ctx.period_start, ctx.period_end
    )
    if not work["measured"]:
        return SectionResult(
            key="work_done", title="Bu dönemde yapılanlar", status="unknown",
            summary="Bu dönem için hareket kaydı yok.",
            data=work,
            unknown_reason=(
                "Dönem içinde tamamlanmış rapor ya da bulgu durumu değişikliği bulunamadı. "
                "Bu, iş yapılmadığı anlamına GELMEZ — dbace yalnızca kendi üzerinden verilen "
                "kararları ve kendi ürettiği bulguları görebiliyor."
            ),
        )

    return SectionResult(
        key="work_done",
        title="Bu dönemde yapılanlar",
        status="ok",
        summary=summarize(work),
        data=work,
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
    """12. Bilinen konular — açık olmayan durumdaki bulgular, kararlarıyla birlikte.

    Ek İŞ A sonrası bu bölüm yalnızca "kabul edilenleri" değil, açık olmayan TÜM durumları
    (yoksayıldı / ertelendi / risk kabul / planlandı) taşıyor. Bulgular asıl bölümlerinden
    çıkarılmıyor — rapor yapısında duruyorlar, sadece burada durumlarıyla listeleniyorlar ki
    "susturulan" şeyler gözden kaybolmasın.
    """
    decisions = (
        await ctx.session.execute(select(FindingAcknowledgement).order_by(FindingAcknowledgement.acknowledged_at.desc()))
    ).scalars().all()
    if not decisions:
        return SectionResult(
            key="known_issues",
            title="Bilinen konular",
            status="ok",
            summary="Açık dışında bir duruma alınmış bulgu yok.",
            data={"items": []},
        )

    now = datetime.now(UTC)
    membership = await build_scope_membership(ctx.session, ctx.instances)
    index = DecisionIndex(list(decisions), membership)

    current: dict[str, FindingDraft] = {}
    for draft in (d for r in results for d in r.findings):
        current[make_fingerprint(draft.section, *draft.fingerprint_parts)] = draft

    items = []
    for fingerprint, draft in current.items():
        finding_type = make_finding_type(draft.section, draft.fingerprint_parts)
        instance_id = draft.related_object_id if draft.related_object_type == "instance" else None
        decision = index.find(fingerprint, finding_type, instance_id)
        if decision is None or decision.status == STATUS_OPEN:
            continue
        effective = resolve_status(decision, still_detected=True, now=now)
        if effective.status == STATUS_OPEN:
            # Süresi dolmuş ya da doğrulanamamış — artık bilinen konu değil, açık bir bulgu.
            continue
        expires = as_utc(decision.expires_at) if decision.expires_at else None
        items.append(
            {
                "fingerprint": fingerprint,
                "finding_type": finding_type,
                "title": draft.title,
                "section": draft.section,
                "severity": draft.severity,
                "status": effective.status,
                "status_label": STATUS_LABELS_TR.get(effective.status, effective.status),
                "scope_type": decision.scope_type,
                "scope_id": decision.scope_id,
                "decided_by": decision.acknowledged_by,
                "decided_at": as_utc(decision.acknowledged_at).isoformat() if decision.acknowledged_at else None,
                "until": expires.isoformat() if expires else None,
                "reference": decision.reference,
                "note": decision.note,
                "days_until_reopen": (expires - now).days if expires else None,
            }
        )

    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1

    return SectionResult(
        key="known_issues",
        title="Bilinen konular",
        status="info" if items else "ok",
        summary=(
            ", ".join(f"{STATUS_LABELS_TR.get(k, k)}: {v}" for k, v in sorted(by_status.items())) + "."
            if items
            else "Açık dışında bir duruma alınmış bulgu yok."
        ),
        data={"items": items, "by_status": by_status},
    )
