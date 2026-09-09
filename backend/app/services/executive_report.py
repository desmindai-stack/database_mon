"""Yönetici raporu (Faz 17 İŞ 3) — müşteriye gösterilen görünüm.

Teknik raporla AYNI veriden türer (aynı `HealthReport` + `ReportFinding` satırları); farkı
derinlik ve dil. Sorulan soru farklı: DBA "bugün ne yapacağım?" diye sorar, yönetici "sistem
sağlıklı mı, ne zaman yatırım gerekecek?" diye.

**Teknik sızıntıya karşı yapısal koruma.** İstenen kural şuydu: ASLA sorgu metni, parametre
adı, komut, log satırı, IP/host detayı. Bunu "dikkat ederiz" diye bırakmak yerine kodda
zorluyoruz:

* Yönetici görünümü bulguların `title`/`detail`/`commands`/`evidence` alanlarını KOPYALAMAZ.
  Cümleler bölüm türüne göre şablonlardan üretilir; bulgudan yalnızca beyaz listeye alınmış
  SAYISAL alanlar (ör. `horizon_days`, `uptime_pct`) okunur.
* Üretilen metin son adımda `assert_no_technical_leak()` ile taranır; SQL anahtar kelimesi,
  komut, host:port ya da dosya yolu içeren bir cümle hata verir (testlerde kanıtlanıyor).

Bu iki katman, ileride biri şablonlara teknik bir alan eklediğinde sessizce sızmasını önler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.finding_status import (
    EXECUTIVE_VISIBLE_DECISIONS,
    STATUS_OPEN,
    STATUS_PLANNED,
    STATUS_RESOLVED,
)
from app.models import (
    Application,
    DatabaseGroup,
    HealthReport,
    Instance,
    Node,
    ReportFinding,
    Server,
)

# Genel sağlık notu.
GRADE_HEALTHY = "Sağlıklı"
GRADE_ATTENTION = "Dikkat"
GRADE_AT_RISK = "Riskli"

RISK_HIGH = "yüksek"
RISK_MEDIUM = "orta"
RISK_LOW = "düşük"

_SEVERITY_TO_RISK = {"critical": RISK_HIGH, "warning": RISK_MEDIUM, "info": RISK_LOW}

# Bölüm → (yönetici diliyle alan adı, risk cümlesi şablonu, iş etkisi).
# Şablonlar yalnızca {target} (uygulama/veritabanı grubu adı) ve beyaz listedeki sayısal
# alanları kullanır — bulgunun teknik başlığı ya da detayı ASLA girmez.
_SECTION_TEMPLATES: dict[str, dict[str, str]] = {
    "availability": {
        "area": "Erişilebilirlik",
        "statement": "{target} sisteminde bu dönemde kesinti yaşandı.",
        "impact": "Kesinti sürelerinde uygulama veritabanına erişemez; işlem kaybı ve kullanıcı şikâyeti riski vardır.",
        "recommendation": "Kesinti nedenlerinin giderilmesi ve izleme altyapısının gözden geçirilmesi.",
    },
    "cluster": {
        "area": "Yüksek erişilebilirlik",
        "statement": "{target} sisteminin küme yapısında beklenmeyen durum değişiklikleri görüldü.",
        "impact": "Küme yapısı, bir sunucu arızasında kesintisiz devam etmeyi sağlar; bu yapının sağlığı bozulursa arıza anında kesinti yaşanabilir.",
        "recommendation": "Küme sağlığının incelenmesi ve gerekiyorsa yedeklilik yapılandırmasının güçlendirilmesi.",
    },
    "performance": {
        "area": "Performans",
        "statement": "{target} sisteminde bazı işlemler bu dönemde yavaşladı.",
        "impact": "Yavaşlayan işlemler uygulama yanıt sürelerini uzatır; yoğun saatlerde kullanıcı deneyimi olumsuz etkilenir.",
        "recommendation": "Yavaşlayan işlemlerin iyileştirilmesi için DBA ekibinin çalışma planına alınması.",
    },
    "resources": {
        "area": "Kaynak kullanımı",
        "statement": "{target} sisteminin kaynak kullanımı sınırlara yaklaştı.",
        "impact": "Kaynaklar dolduğunda yeni işlemler reddedilir; kapasite artırımı gerekebilir.",
        "recommendation": "Kaynak kullanımının izlenmesi ve gerekirse kapasite artırımının planlanması.",
    },
    "schema": {
        "area": "Veri büyümesi",
        "statement": "{target} sisteminde veri hacmi hızla artıyor.",
        "impact": "Kontrolsüz veri büyümesi depolama maliyetini artırır ve uzun vadede performansı düşürür.",
        "recommendation": "Veri saklama (arşivleme) politikasının gözden geçirilmesi.",
    },
    "alerts": {
        "area": "İzleme",
        "statement": "{target} sistemi için açık kalan uyarılar var.",
        "impact": "Uzun süre açık kalan uyarılar, gerçek sorunların gözden kaçmasına yol açabilir.",
        "recommendation": "Açık uyarıların kapatılması ve uyarı eşiklerinin gözden geçirilmesi.",
    },
    "capacity": {
        "area": "Kapasite",
        "statement": "{target} sisteminin depolama/kapasite sınırına yaklaşması bekleniyor.",
        "impact": "Kapasite dolduğunda veritabanı yeni kayıt kabul edemez; bu, plansız bir kesinti demektir.",
        "recommendation": "Kapasite artırımının bütçelenmesi ve planlanması.",
    },
    "parameters": {
        "area": "Yapılandırma",
        "statement": "{target} sisteminin yapılandırmasında önerilen değerlerden sapmalar var.",
        "impact": "Yapılandırma sapmaları performans ve veri güvenliği açısından risk oluşturabilir.",
        "recommendation": "Yapılandırmanın standartlara göre düzeltilmesi.",
    },
    "sla": {
        "area": "Hizmet seviyesi",
        "statement": "{target} sistemi için taahhüt edilen erişilebilirlik hedefi bu dönemde risk altında.",
        "impact": "Hedefin altında kalınması sözleşmesel taahhüdün karşılanmaması demektir; kesinti süresi doğrudan iş kaybına karşılık gelir.",
        "recommendation": "Kalan dönemde riskli değişikliklerin ertelenmesi ve kesinti nedenlerinin giderilmesi.",
    },
    "backup": {
        "area": "Yedek güvencesi",
        "statement": "{target} sisteminde yedek durumu dikkat gerektiriyor.",
        "impact": "Güncel bir yedek olmadan, bir arıza anında en son yedekten bu yana yapılan tüm işlemler geri getirilemez.",
        "recommendation": "Yedek alma düzeninin gözden geçirilmesi ve geri dönüş tatbikatı yapılması.",
    },
    "prerequisites": {
        "area": "İzleme kapsamı",
        "statement": "{target} sisteminde bazı izleme özellikleri devre dışı.",
        "impact": "Eksik izleme, sorunların erken fark edilmesini engeller.",
        "recommendation": "İzleme için gerekli ayarların etkinleştirilmesi.",
    },
}

_DEFAULT_TEMPLATE = {
    "area": "Genel",
    "statement": "{target} sisteminde dikkat gerektiren bir durum tespit edildi.",
    "impact": "Ayrıntılar teknik raporda yer alıyor.",
    "recommendation": "DBA ekibinin incelemesi.",
}

# Yönetici metnine asla girmemesi gereken kalıplar. Savunma katmanı — şablonlar zaten teknik
# alan kopyalamıyor, bu tarama ileride biri yanlışlıkla eklerse yakalar.
_FORBIDDEN_PATTERNS = [
    re.compile(r"\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bALTER\b|\bCREATE\b|\bDROP\b|\bVACUUM\b|\bREINDEX\b", re.I),
    re.compile(r"\bpg_[a-z_]+\b", re.I),          # pg_stat_statements, pg_settings, ...
    re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"),      # IPv4
    re.compile(r"[a-z0-9_.-]+:\d{2,5}\b", re.I),   # host:port
    re.compile(r"(^|\s)(/|[A-Za-z]:\\)[\w./\\-]+"),  # dosya yolu
    re.compile(r"\bshared_buffers\b|\bwork_mem\b|\bmax_connections\b|\bautovacuum\w*\b", re.I),
]


class TechnicalLeakError(ValueError):
    """Yönetici raporunda teknik detay tespit edildi — üretim durdurulur."""


def assert_no_technical_leak(text: str, where: str = "") -> None:
    for pattern in _FORBIDDEN_PATTERNS:
        match = pattern.search(text or "")
        if match:
            raise TechnicalLeakError(
                f"Yönetici raporuna teknik detay sızdı ({where}): {match.group(0)!r}"
            )


@dataclass
class ExecutiveReport:
    scope_label: str
    period_start: datetime
    period_end: datetime
    period_label: str
    generated_at: datetime
    grade: str
    grade_reason: str
    availability: dict[str, Any] = field(default_factory=dict)
    inventory: dict[str, Any] = field(default_factory=dict)
    risks: list[dict[str, Any]] = field(default_factory=list)
    trend: dict[str, Any] = field(default_factory=dict)
    work_done: dict[str, Any] = field(default_factory=dict)
    # Faz 28 İŞ 1b: yedek güvencesi. Erişilebilirlik gibi, bulgu OLMASA DA gösterilen bir
    # ölçü — yöneticinin sorduğu soru "sorun var mı" değil "yedeğim var mı" ve bu sorunun
    # cevabı yalnızca kötü haber olduğunda görünürse rapor güvence vermiyor demektir.
    backup: dict[str, Any] = field(default_factory=dict)
    # Faz 28 İŞ 3b: SLA durumu. "Kalan kesinti bütçesi" yöneticiye çıplak yüzdeden çok daha
    # anlamlı: "47 dakikanız kaldı" cümlesi bakım planlamak için doğrudan kullanılabilir.
    sla: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    # Ek İŞ A: "planlandı" ve "risk kabul" durumundaki konular. Biri ekibin çalıştığını,
    # diğeri bilinçli bir kararı gösterir — ikisi de yöneticinin bilmesi gereken şeyler.
    # "Yoksayıldı" BURAYA GİRMEZ: o, ekibin kendi iç gürültü yönetimi kararıdır.
    decisions: list[dict[str, Any]] = field(default_factory=list)


def _fmt_duration(seconds: float | None) -> str:
    """Yönetici diliyle süre. Teknik birim yok, saniye/dakika/saat/gün."""
    if seconds is None:
        return "—"
    value = abs(float(seconds))
    if value < 90:
        return f"{value:.0f} saniye"
    if value < 5400:
        return f"{value / 60:.0f} dakika"
    if value < 172800:
        return f"{value / 3600:.1f} saat"
    return f"{value / 86400:.1f} gün"


def _period_label(start: datetime, end: datetime) -> str:
    days = max(round((end - start).total_seconds() / 86400), 1)
    if days <= 1:
        return "Günlük"
    if days <= 7:
        return "Haftalık"
    if days <= 31:
        return "Aylık"
    return f"{days} günlük"


def _grade(critical: int, warning: int, uptime: float | None) -> tuple[str, str]:
    if critical or (uptime is not None and uptime < 99.0):
        reason = (
            "Acil müdahale gerektiren bulgular var."
            if critical
            else "Bu dönemde erişilebilirlik hedefin altında kaldı."
        )
        return GRADE_AT_RISK, reason
    if warning:
        return GRADE_ATTENTION, "Acil olmayan ancak takip edilmesi gereken konular var."
    return GRADE_HEALTHY, "Bu dönemde dikkat gerektiren bir bulgu tespit edilmedi."


def _safe_label(label: str, fallback: str) -> str:
    """Müşteri tarafından verilmiş bir ad (uygulama/grup adı) teknik bir kalıba benziyorsa
    (ör. bir IP ya da host:port gibi adlandırılmışsa) genel bir etikete düşürülür.

    Alternatif, raporu tamamen hata verdirmekti; bir uygulamanın kötü adlandırılmış olması
    yöneticinin raporu hiç görememesine yol açmamalı.
    """
    try:
        assert_no_technical_leak(label, "label")
    except TechnicalLeakError:
        return fallback
    return label


async def _target_label(session: AsyncSession, finding: ReportFinding, fallback: str) -> str:
    """Bulgunun hedefi için müşteriye gösterilebilir ad.

    Sunucu adı/host DEĞİL: mümkünse uygulama adı, olmazsa veritabanı grubu adı. Instance adı
    çoğu kurulumda sunucu adını içerdiği için son çare olarak bile kullanılmıyor.
    """
    if finding.related_object_type == "instance" and finding.related_object_id:
        instance = await session.get(Instance, finding.related_object_id)
        if instance is not None:
            if instance.group_id:
                group = await session.get(DatabaseGroup, instance.group_id)
                if group is not None:
                    application = await session.get(Application, group.application_id)
                    if application is not None:
                        return application.name
                    return group.name
            if instance.application:
                return instance.application
    if finding.related_object_type == "group" and finding.related_object_id:
        group = await session.get(DatabaseGroup, finding.related_object_id)
        if group is not None:
            application = await session.get(Application, group.application_id)
            return application.name if application else group.name
    return fallback


async def _inventory(session: AsyncSession, instances: list[Instance]) -> dict[str, Any]:
    group_ids = sorted({i.group_id for i in instances if i.group_id})
    groups: list[DatabaseGroup] = []
    if group_ids:
        groups = list(
            (await session.execute(select(DatabaseGroup).where(DatabaseGroup.id.in_(group_ids)))).scalars().all()
        )

    dr_covered = 0
    for group in groups:
        # Site bilgisi düğümde değil sunucuda (Server.site) — DR kapsamı oradan okunur.
        server_ids = list(
            (
                await session.execute(
                    select(Node.server_id).where(Node.group_id == group.id, Node.server_id.is_not(None))
                )
            ).scalars().all()
        )
        if not server_ids:
            continue
        sites = set(
            (await session.execute(select(Server.site).where(Server.id.in_(server_ids)))).scalars().all()
        )
        if "disaster" in sites:
            dr_covered += 1

    environments: dict[str, int] = {}
    for group in groups:
        environments[group.environment] = environments.get(group.environment, 0) + 1
    for instance in instances:
        if not instance.group_id:
            environments[instance.environment] = environments.get(instance.environment, 0) + 1

    topologies: dict[str, int] = {}
    for group in groups:
        topologies[group.topology] = topologies.get(group.topology, 0) + 1

    return {
        "database_count": len(instances),
        "group_count": len(groups),
        "environments": environments,
        "topologies": topologies,
        "dr_covered_groups": dr_covered,
        "dr_coverage_note": (
            f"{dr_covered}/{len(groups)} veritabanı kümesinin felaket kurtarma (ikinci merkez) kapsamı var."
            if groups
            else "Küme yapısı tanımlı veritabanı yok."
        ),
    }


def _availability_from_sections(sections: dict[str, Any]) -> dict[str, Any]:
    """Erişilebilirlik özetini teknik bölümün VERİSİNDEN türetir — metnini değil.

    Teknik bölümün cümleleri sunucu adı ve ölçüm yöntemi içerir; buradan yalnızca sayılar
    alınır ve müşteri diliyle yeniden yazılır.
    """
    item = ((sections or {}).get("items") or {}).get("availability") or {}
    data = item.get("data") or {}
    rows = data.get("instances") or []
    measured = [r for r in rows if r.get("uptime_pct") is not None]
    if not measured:
        return {
            "uptime_pct": None,
            "outage_count": 0,
            "total_outage_seconds": 0.0,
            "longest_outage_seconds": 0.0,
            "unknown_reason": "Bu dönem için erişilebilirlik ölçümü yapılamadı.",
            "by_application": [],
        }
    return {
        "uptime_pct": round(sum(r["uptime_pct"] for r in measured) / len(measured), 3),
        "outage_count": sum(r.get("outage_count", 0) for r in rows),
        "total_outage_seconds": round(sum(r.get("outage_seconds", 0.0) for r in rows), 1),
        "longest_outage_seconds": round(max((r.get("longest_outage_seconds", 0.0) for r in rows), default=0.0), 1),
        "unknown_reason": None,
        "by_application": [],
    }


def _backup_from_sections(sections: dict[str, Any]) -> dict[str, Any]:
    """Yedek güvencesi özetini teknik bölümün VERİSİNDEN türetir.

    Kullanıcının istediği cümle: "son yedek X gün önce, SLA'ya uygun/uygun değil". Bunun için
    teknik bölümün metni değil yalnızca SAYILARI okunuyor — teknik bölüm sunucu adı, kaynak
    adı ve komut içeriyor, hiçbiri yönetici raporuna giremez.

    ÜÇÜNCÜ DURUM ÖNEMLİ: `sla_ok` üç değerli — uygun, uygun değil ve **belirlenemedi**.
    Belirlenemedi'yi "uygun değil" saymak yanlış alarm, "uygun" saymak sahte güvence olurdu;
    ikisi de yöneticiye yanlış bir karar verdirir.
    """
    item = ((sections or {}).get("items") or {}).get("backup") or {}
    rows = (item.get("data") or {}).get("instances") or []
    if not rows:
        return {
            "measured": False,
            "statement": "Bu dönemde yedek durumu değerlendirilemedi.",
            "sla_ok": None,
            "database_count": 0,
            "protected_count": 0,
            "breached_count": 0,
            "unknown_count": 0,
            "oldest_backup_days": None,
        }

    protected = [r for r in rows if r.get("sla_ok") is True]
    breached = [r for r in rows if r.get("sla_ok") is False]
    unknown = [r for r in rows if r.get("sla_ok") is None]
    ages = [
        r["last_full_age_hours"] / 24.0
        for r in rows
        if isinstance(r.get("last_full_age_hours"), (int, float))
    ]
    # EN ESKİ yedek belirleyici, ortalama değil: on veritabanından dokuzunun yedeği dünse ve
    # birininki 40 günse ortalama "4 gün" der ve gerçek riski gizler.
    oldest = max(ages) if ages else None

    if breached:
        statement = (
            f"{len(breached)} veritabanında en son yedek hedeflenen sıklığın dışında kaldı"
            + (f"; en eskisi {oldest:.0f} gün önce alınmış." if oldest is not None else ".")
        )
        sla_ok: bool | None = False
    elif not protected and unknown:
        statement = (
            f"{len(unknown)} veritabanının yedek durumu belirlenemedi; bu, yedek alınmadığı "
            "anlamına gelmez ancak güvence de verilemez."
        )
        sla_ok = None
    else:
        statement = (
            "Tüm veritabanlarında yedekler hedeflenen sıklıkta"
            + (f"; en eskisi {oldest:.0f} gün önce alınmış." if oldest is not None else ".")
        )
        if unknown:
            statement += f" {len(unknown)} veritabanında durum belirlenemedi."
        sla_ok = True

    return {
        "measured": True,
        "statement": statement,
        "sla_ok": sla_ok,
        "database_count": len(rows),
        "protected_count": len(protected),
        "breached_count": len(breached),
        "unknown_count": len(unknown),
        "oldest_backup_days": round(oldest, 1) if oldest is not None else None,
    }


def _sla_from_sections(sections: dict[str, Any]) -> list[dict[str, Any]]:
    """SLA özetini teknik bölümün VERİSİNDEN türetir — teknik detay taşımadan.

    Yöneticiye giden alanlar bilinçli olarak dar: kapsam adı, hedef, gerçekleşen, kalan
    bütçe ve "tutuyor mu". Sunucu adı ("en kötü veritabanı") DIŞARIDA bırakılıyor; teknik
    raporda duruyor.
    """
    item = ((sections or {}).get("items") or {}).get("sla") or {}
    rows = (item.get("data") or {}).get("targets") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("measured"):
            out.append(
                {
                    "scope_label": row.get("scope_label") or "İzlenen sistem",
                    "period_label": row.get("period_label"),
                    "target_pct": row.get("target_pct"),
                    "achieved_pct": None,
                    "met": None,
                    "statement": "Bu dönem için erişilebilirlik ölçülemedi; hedefe uygunluk belirlenemiyor.",
                }
            )
            continue
        budget = row.get("remaining_budget_seconds")
        if row.get("already_lost"):
            statement = (
                f"Hedef %{row['target_pct']}; kalan süre kesintisiz geçse bile "
                f"en fazla %{row['best_case_pct']} olabilir — hedef bu dönem karşılanamayacak."
            )
        elif row.get("met"):
            statement = (
                f"Hedef %{row['target_pct']}, gerçekleşen %{row['achieved_pct']} — hedefe uygun."
                + (
                    f" Kalan kesinti bütçesi: {_fmt_duration(budget)}."
                    if isinstance(budget, (int, float)) and budget > 0
                    else ""
                )
            )
        else:
            statement = (
                f"Hedef %{row['target_pct']}, gerçekleşen %{row['achieved_pct']} — hedefin altında."
            )
        out.append(
            {
                "scope_label": row.get("scope_label") or "İzlenen sistem",
                "period_label": row.get("period_label"),
                "target_pct": row.get("target_pct"),
                "achieved_pct": row.get("achieved_pct"),
                "best_case_pct": row.get("best_case_pct"),
                "remaining_budget_seconds": budget,
                "met": row.get("met"),
                "already_lost": row.get("already_lost", False),
                "planned_seconds": row.get("planned_seconds"),
                "unplanned_seconds": row.get("unplanned_seconds"),
                "statement": statement,
            }
        )
    return out


async def build_executive_report(session: AsyncSession, report: HealthReport) -> ExecutiveReport:
    findings = list(
        (
            await session.execute(
                select(ReportFinding)
                .where(ReportFinding.report_id == report.id)
                .order_by(ReportFinding.priority.desc())
            )
        ).scalars().all()
    )
    # Ek İŞ A: yalnızca "açık" bulgular riske dönüşür.
    open_findings = [f for f in findings if f.status == STATUS_OPEN and f.change_state != "resolved"]
    resolved = [f for f in findings if f.change_state == "resolved" or f.status == STATUS_RESOLVED]

    critical = sum(1 for f in open_findings if f.severity == "critical")
    warning = sum(1 for f in open_findings if f.severity == "warning")

    from app.services.health_report import ReportScope, resolve_scope_instances

    instances = await resolve_scope_instances(
        session, ReportScope(report.scope_type, report.scope_id, report.scope_label)
    )

    availability = _availability_from_sections(report.sections or {})
    backup = _backup_from_sections(report.sections or {})
    sla = _sla_from_sections(report.sections or {})
    inventory = await _inventory(session, instances)

    # Uygulama bazında erişilebilirlik: teknik bölümün instance satırlarını uygulama adına
    # göre toplar (sunucu adı dışarı çıkmaz).
    rows = (((report.sections or {}).get("items") or {}).get("availability") or {}).get("data", {}).get("instances", [])
    by_app: dict[str, dict[str, Any]] = {}
    for row in rows:
        instance = next((i for i in instances if i.id == row.get("instance_id")), None)
        label = report.scope_label
        if instance is not None and instance.group_id:
            group = await session.get(DatabaseGroup, instance.group_id)
            if group is not None:
                application = await session.get(Application, group.application_id)
                label = application.name if application else group.name
        elif instance is not None and instance.application:
            label = instance.application
        bucket = by_app.setdefault(
            label, {"application": label, "uptime_values": [], "outage_count": 0, "outage_seconds": 0.0}
        )
        if row.get("uptime_pct") is not None:
            bucket["uptime_values"].append(row["uptime_pct"])
        bucket["outage_count"] += row.get("outage_count", 0)
        bucket["outage_seconds"] += row.get("outage_seconds", 0.0)
    availability["by_application"] = [
        {
            "application": b["application"],
            "uptime_pct": round(sum(b["uptime_values"]) / len(b["uptime_values"]), 3) if b["uptime_values"] else None,
            "outage_count": b["outage_count"],
            "outage_seconds": round(b["outage_seconds"], 1),
        }
        for b in by_app.values()
    ]

    grade, grade_reason = _grade(critical, warning, availability.get("uptime_pct"))

    # Risk özeti: bulgu başına bir madde, ŞABLONDAN üretilmiş cümlelerle.
    risks: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for finding in open_findings:
        template = _SECTION_TEMPLATES.get(finding.section, _DEFAULT_TEMPLATE)
        target = _safe_label(await _target_label(session, finding, report.scope_label), "İzlenen sistem")
        key = (finding.section, target)
        if key in seen_keys:
            continue  # aynı alan + aynı uygulama için tek madde — yönetici listesi kısa olmalı
        seen_keys.add(key)

        statement = template["statement"].format(target=target)
        # Beyaz listeye alınmış sayısal alanlar cümleyi somutlaştırır (ör. "45 gün içinde").
        evidence = finding.evidence or {}
        horizon = evidence.get("horizon_days")
        if finding.section == "capacity" and isinstance(horizon, (int, float)) and horizon > 0:
            statement = (
                f"{target} sisteminin kapasite sınırına yaklaşık {int(horizon)} gün içinde "
                "ulaşması bekleniyor."
            )

        risks.append(
            {
                "level": _SEVERITY_TO_RISK.get(finding.severity, RISK_LOW),
                "area": template["area"],
                "application": target,
                "statement": statement,
                "business_impact": template["impact"],
                "open_since_days": finding.open_since_days,
            }
        )

    recommendations = []
    for risk in risks:
        if risk["level"] == RISK_LOW:
            continue
        template = next(
            (t for t in _SECTION_TEMPLATES.values() if t["area"] == risk["area"]), _DEFAULT_TEMPLATE
        )
        recommendations.append(
            {
                "priority": risk["level"],
                "area": risk["area"],
                "application": risk["application"],
                "action": template["recommendation"],
                "if_not_done": risk["business_impact"],
            }
        )

    trend: dict[str, Any] = {"available": False, "note": "Karşılaştırılacak önceki dönem raporu yok."}
    if report.previous_report_id:
        previous = await session.get(HealthReport, report.previous_report_id)
        if previous is not None:
            previous_findings = list(
                (
                    await session.execute(
                        select(ReportFinding).where(ReportFinding.report_id == previous.id)
                    )
                ).scalars().all()
            )
            previous_open = [
                f for f in previous_findings if not f.acknowledged and f.change_state != "resolved"
            ]
            previous_critical = sum(1 for f in previous_open if f.severity == "critical")
            previous_warning = sum(1 for f in previous_open if f.severity == "warning")
            direction = (
                "iyileşti"
                if (critical + warning) < (previous_critical + previous_warning)
                else "kötüleşti"
                if (critical + warning) > (previous_critical + previous_warning)
                else "değişmedi"
            )
            previous_uptime = _availability_from_sections(previous.sections or {}).get("uptime_pct")
            trend = {
                "available": True,
                "direction": direction,
                "current": {"critical": critical, "warning": warning, "uptime_pct": availability.get("uptime_pct")},
                "previous": {
                    "critical": previous_critical,
                    "warning": previous_warning,
                    "uptime_pct": previous_uptime,
                },
                "note": f"Önceki döneme göre genel durum {direction}.",
            }

    work_done = {
        "closed_findings": len(resolved),
        "note": (
            f"Bu dönemde {len(resolved)} konu kapatıldı."
            if resolved
            else "Bu dönemde kapatılan bir konu yok."
        ),
    }

    # "Planlandı" / "risk kabul" konuları — teknik detay olmadan, iş etkisiyle.
    decision_rows: list[dict[str, Any]] = []
    decision_seen: set[tuple[str, str]] = set()
    for finding in findings:
        if finding.status not in EXECUTIVE_VISIBLE_DECISIONS:
            continue
        template = _SECTION_TEMPLATES.get(finding.section, _DEFAULT_TEMPLATE)
        target = _safe_label(await _target_label(session, finding, report.scope_label), "İzlenen sistem")
        key = (finding.status, template["area"] + target)
        if key in decision_seen:
            continue
        decision_seen.add(key)
        decision_rows.append(
            {
                "kind": "planned" if finding.status == STATUS_PLANNED else "risk_accepted",
                "label": "Çalışma planlandı" if finding.status == STATUS_PLANNED else "Risk bilinçli kabul edildi",
                "area": template["area"],
                "application": target,
                "statement": template["statement"].format(target=target),
                "business_impact": template["impact"],
                # Referans (ticket/CR no) yöneticinin takip edebilmesi için tek teknik olmayan
                # tanımlayıcı — serbest metin, sunucu/sorgu bilgisi içermez.
                "reference": finding.decision_reference,
            }
        )

    executive = ExecutiveReport(
        scope_label=report.scope_label,
        period_start=report.period_start,
        period_end=report.period_end,
        period_label=_period_label(report.period_start, report.period_end),
        generated_at=report.generated_at or datetime.now(UTC),
        grade=grade,
        grade_reason=grade_reason,
        availability=availability,
        backup=backup,
        sla=sla,
        inventory=inventory,
        risks=risks,
        trend=trend,
        work_done=work_done,
        recommendations=recommendations,
        decisions=decision_rows,
    )

    # Savunma katmanı: üretilen tüm serbest metin taranır.
    for risk in executive.risks:
        assert_no_technical_leak(risk["statement"], "risk.statement")
        assert_no_technical_leak(risk["business_impact"], "risk.business_impact")
        assert_no_technical_leak(risk["area"], "risk.area")
        # risk["application"] zaten _safe_label'dan geçti; ayrıca taranmıyor.
    for recommendation in executive.recommendations:
        assert_no_technical_leak(recommendation["action"], "recommendation.action")
        assert_no_technical_leak(recommendation["if_not_done"], "recommendation.if_not_done")
    assert_no_technical_leak(executive.grade_reason, "grade_reason")
    assert_no_technical_leak(executive.backup["statement"], "backup.statement")
    for row in executive.sla:
        assert_no_technical_leak(row["statement"], "sla.statement")
        assert_no_technical_leak(row["scope_label"], "sla.scope_label")
    assert_no_technical_leak(executive.work_done["note"], "work_done.note")
    for decision in executive.decisions:
        assert_no_technical_leak(decision["statement"], "decision.statement")
        assert_no_technical_leak(decision["business_impact"], "decision.business_impact")
        if decision["reference"]:
            assert_no_technical_leak(decision["reference"], "decision.reference")

    return executive
