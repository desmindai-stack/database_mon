"""Sağlık Raporu motoru (Faz 17 İŞ 1).

Tek bir toplanmış veri kümesinden iki farklı rapor üretilir: teknik (DBA) ve yönetici
(müşteri). Bu modül her ikisinin de dayandığı ORTAK üretim hattıdır — bölümleri çalıştırır,
bulguları fingerprint'leyip önceki raporla karşılaştırır, kabul edilmiş (acknowledged)
bulguları ayırır, etki × aciliyet ile sıralar ve sonucu HealthReport/ReportFinding olarak
saklar.

Tasarım kararları:

* **Canlı probe yok.** Rapor yalnızca saklanmış veriden (MetricSample, SlowQuerySample,
  AlertEvent, PredictionInsight, GroupHealthSnapshot, rollup tabloları, Instance üzerindeki
  collector alanları) üretilir. Rapor 06:00'da onlarca instance için çalışır; her biri için
  canlı bağlantı açmak toplama döngüsüyle yarışırdı ve "dün ne oldu" sorusunu "şu an ne
  oluyor"a çevirirdi.
* **Arka planda üretim.** HealthReport satırı `status="running"` ile ÖNCE oluşturulur, bölümler
  ilerledikçe `progress_pct`/`progress_label` güncellenir. Arayüz ilerlemeyi buradan okur.
* **Kanıtsız bulgu yok.** `FindingDraft.evidence` zorunlu; boş bırakan bir bölüm üretim
  sırasında hata alır (Faz 17 İŞ 6 kalite kuralı, kodda zorlanıyor).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.services.advice import Advice, advice_to_dict, simple_advice, unavailable
from app.services.finding_dependencies import SuppressionPlan, build_suppression_plan
from app.services.finding_status import (
    COUNTED_STATUSES,
    STATUS_OPEN,
    STATUS_RESOLVED,
    DecisionIndex,
    build_scope_membership,
    decision_payload,
    load_decisions,
    make_finding_type,
    record_history,
    resolve_status,
)
from app.models import (
    Application,
    Customer,
    DatabaseGroup,
    HealthReport,
    Instance,
    ReportFinding,
)

logger = logging.getLogger(__name__)

SCOPE_TYPES = ("global", "customer", "application", "group", "instance")

SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1, "ok": 0}

# Etki × aciliyet sıralamasının ağırlıkları (Faz 17 İŞ 6: "alfabetik veya rastgele değil").
_SEVERITY_WEIGHT = {"critical": 100.0, "warning": 40.0, "info": 10.0, "ok": 0.0}
# Prod ortam preprod/test'ten önce gelir — aynı ciddiyetteki iki bulgudan prod olanı üstte.
_ENVIRONMENT_WEIGHT = {"prod": 1.6, "production": 1.6, "preprod": 1.1, "test": 0.9, "dev": 0.8}
_CHANGE_WEIGHT = {"regressed": 1.35, "new": 1.2, "ongoing": 1.0, "resolved": 0.3}


@dataclass
class FindingDraft:
    """Bölümlerin ürettiği ham bulgu. fingerprint motorda hesaplanır."""

    section: str
    severity: str
    title: str
    detail: str
    # Bulgunun dayandığı ölçüm: {"metric": ..., "value": ..., "threshold": ..., "measured_at": ...}
    evidence: dict[str, Any]
    fingerprint_parts: tuple[str, ...]
    recommendation: str | None = None
    commands: list[str] = field(default_factory=list)
    related_object_type: str | None = None
    related_object_id: int | None = None
    # Faz 18 İŞ 4: bulgunun sayısal özeti — [{label, value, tone}]. Uzun bir paragraf yerine
    # "ne kadar / neye göre" bilgisi etiketli ve vurgulu satırlar olarak gösteriliyor; `detail`
    # ise yalnızca "ne oldu" cümlesi olarak kısa kalıyor.
    facts: list[dict[str, Any]] = field(default_factory=list)
    # Faz 18 İŞ 3: kısa sınırlılık notu — "darboğaz belirlenemedi çünkü şu veri yok" gibi
    # bilgiler bulgu metninin içine gömülü uzun bir cümle olarak değil, ayrı ve kısa bir not
    # olarak gösteriliyor; bulgunun kendisinin önüne geçmesin.
    note: str | None = None
    # Faz 18 İŞ 1: bulgunun işaret ettiği KESİN hedef (sorgu anahtarı, pencere, sekme dahil).
    # Boşsa arayüz bölüm→sekme eşlemesinden genel bir bağlantı üretir; dolu olduğunda bulgu
    # tam olarak bahsettiği veriye götürür.
    link_hint: str | None = None
    # Ortam etiketi (prod/preprod/test) — öncelik sıralamasında kullanılır.
    environment: str = "prod"
    # Faz 17 Ek İŞ B: standart öneri yapısı. Bölüm doldurmazsa motor `recommendation` ve
    # `commands` alanlarından asgari bir yapı üretir — böylece arayüz her bulguda AYNI
    # şekli görür, bölümlerin hepsini aynı anda güncellemek gerekmez.
    advice: "Advice | None" = None


@dataclass
class SectionResult:
    """Bir rapor bölümünün çıktısı."""

    key: str
    title: str
    status: str  # ok | info | warning | critical | unknown
    summary: str
    findings: list[FindingDraft] = field(default_factory=list)
    # Bölüme özgü serbest veri (tablolar, grafik serileri) — sections JSON'una gider.
    data: dict[str, Any] = field(default_factory=dict)
    # Veri yetersizse burada söylenir; "sorunsuz" gibi gösterilmez (Faz 17 İŞ 6 dürüstlük kuralı).
    unknown_reason: str | None = None


@dataclass
class ReportScope:
    scope_type: str
    scope_id: int | None
    label: str


@dataclass
class ReportContext:
    session: AsyncSession
    scope: ReportScope
    instances: list[Instance]
    period_start: datetime
    period_end: datetime
    previous: HealthReport | None
    previous_findings: dict[str, ReportFinding]

    # Faz 28 İŞ 2 — bağımlılık bastırma planı. Normal bölümler bittikten SONRA, özet
    # bölümleri çalışmadan ÖNCE dolduruluyor.
    #
    # Neden context'te: yönetici özeti de, kaydedilen bulgular da, sayaçlar da aynı planı
    # kullanmak zorunda. İkisi ayrı hesaplasaydı özet "40 kritik" derken liste 1 kritik
    # gösterirdi — CLAUDE.md'deki "aynı veriyi gösteren yerler tek kaynaktan beslensin"
    # kuralının tam olarak uyardığı durum.
    suppression: "SuppressionPlan | None" = None
    unique_drafts: list["FindingDraft"] = field(default_factory=list)
    draft_fingerprints: dict[int, str] = field(default_factory=dict)

    @property
    def period_days(self) -> float:
        return max((self.period_end - self.period_start).total_seconds() / 86400.0, 0.0)

    def instance_ids(self) -> list[int]:
        return [i.id for i in self.instances]

    def instance_by_id(self, instance_id: int) -> Instance | None:
        return next((i for i in self.instances if i.id == instance_id), None)


SectionBuilder = Callable[[ReportContext], Awaitable[SectionResult]]
# Özet bölümleri diğer bölümlerin ÇIKTISINI görür (yönetici özeti, değişenler, bilinen konular).
SummaryBuilder = Callable[[ReportContext, list["SectionResult"]], Awaitable["SectionResult"]]

_SECTION_BUILDERS: list[SectionBuilder] = []
_SUMMARY_BUILDERS: list[SummaryBuilder] = []


def register_section(builder: SectionBuilder) -> SectionBuilder:
    """Normal bölüm. Kayıt sırası, raporda görünme sırasıdır."""
    _SECTION_BUILDERS.append(builder)
    return builder


def register_summary_section(builder: SummaryBuilder) -> SummaryBuilder:
    """Diğer bölümlerin sonucuna bakan bölüm (yönetici özeti, dünden beri değişenler, bilinen
    konular). SONRA çalışır ama raporda ÖNDE görünür — özet, özetlediği şeyin üstünde durmalı."""
    _SUMMARY_BUILDERS.append(builder)
    return builder


def registered_sections() -> list[SectionBuilder]:
    return list(_SECTION_BUILDERS)


def registered_summary_sections() -> list[SummaryBuilder]:
    return list(_SUMMARY_BUILDERS)


def as_utc(value: datetime) -> datetime:
    """SQLite naive, Postgres aware datetime döndürür — karşılaştırmalar iki motorda da
    çalışsın diye tek noktada UTC'ye sabitleniyor."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def make_fingerprint(*parts: str) -> str:
    """Bulgunun kararlı kimliği.

    ÖLÇÜLEN DEĞER kasıtlı olarak dışarıda: hash'e girseydi değer her değiştiğinde (yani her
    gün) bulgu "yeni" görünür, "kaç gündür açık" sayacı hiç ilerlemez ve gürültü kontrolü
    (Faz 17 İŞ 6) çalışmazdı. Hash'e sadece bulgu TİPİ ve HEDEF NESNE girer.
    """
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def resolve_scope_instances(session: AsyncSession, scope: ReportScope) -> list[Instance]:
    """Kapsamın kapsadığı instance'lar.

    Gruplu instance'lar grup → uygulama → müşteri zinciriyle bulunur; gruba bağlanmamış eski
    instance'lar (geriye dönük uyumluluk, `Instance.customer_name`) müşteri kapsamında ad
    eşleşmesiyle dahil edilir — aksi halde tek başına eklenmiş bir sunucu hiçbir müşteri
    raporunda görünmezdi.
    """
    stmt = select(Instance).where(Instance.enabled.is_(True))

    if scope.scope_type == "instance":
        stmt = stmt.where(Instance.id == scope.scope_id)
    elif scope.scope_type == "group":
        stmt = stmt.where(Instance.group_id == scope.scope_id)
    elif scope.scope_type == "application":
        group_ids = (
            await session.execute(select(DatabaseGroup.id).where(DatabaseGroup.application_id == scope.scope_id))
        ).scalars().all()
        stmt = stmt.where(Instance.group_id.in_(list(group_ids) or [-1]))
    elif scope.scope_type == "customer":
        app_ids = (
            await session.execute(select(Application.id).where(Application.customer_id == scope.scope_id))
        ).scalars().all()
        group_ids = (
            await session.execute(
                select(DatabaseGroup.id).where(DatabaseGroup.application_id.in_(list(app_ids) or [-1]))
            )
        ).scalars().all()
        customer = await session.get(Customer, scope.scope_id)
        conditions = Instance.group_id.in_(list(group_ids) or [-1])
        if customer is not None:
            conditions = conditions | (Instance.customer_name == customer.name)
        stmt = stmt.where(conditions)

    rows = (await session.execute(stmt.options(selectinload(Instance.group)))).scalars().all()
    return list(rows)


async def resolve_scope_label(session: AsyncSession, scope_type: str, scope_id: int | None) -> str:
    if scope_type == "global":
        return "Tüm sistem"
    if scope_id is None:
        return scope_type
    if scope_type == "customer":
        row = await session.get(Customer, scope_id)
    elif scope_type == "application":
        row = await session.get(Application, scope_id)
    elif scope_type == "group":
        row = await session.get(DatabaseGroup, scope_id)
    else:
        row = await session.get(Instance, scope_id)
    return getattr(row, "name", None) or f"{scope_type} #{scope_id}"


async def _previous_report(session: AsyncSession, scope: ReportScope, before: datetime) -> HealthReport | None:
    stmt = (
        select(HealthReport)
        .where(
            HealthReport.scope_type == scope.scope_type,
            HealthReport.status == "done",
            HealthReport.generated_at < before,
        )
        # id ikinci sıralama ölçütü: SQLite'ın CURRENT_TIMESTAMP'i SANİYE hassasiyetinde,
        # yani aynı saniyede üretilen iki rapor eşitlenir ve "önceki rapor" zinciri kopar
        # ("kaç gündür açık" sayacı ilerlemez). id her zaman artan olduğu için beraberliği
        # doğru yönde bozar.
        .order_by(HealthReport.generated_at.desc(), HealthReport.id.desc())
        .limit(1)
    )
    stmt = stmt.where(HealthReport.scope_id.is_(None) if scope.scope_id is None else HealthReport.scope_id == scope.scope_id)
    return (await session.execute(stmt)).scalar_one_or_none()


#: Kök sebep bulgusunun öncelik çarpanı (Faz 28 İŞ 2).
#:
#: Kök sebep, kendisiyle aynı ciddiyetteki başka bir kritik bulgunun ALTINDA kalmamalı:
#: 39 bulguyu doğuran şey listenin ortasında duruyorsa, bastırma işini yarısına kadar
#: yapmış oluruz. Çarpan büyük ama sonsuz değil — bilgi seviyesindeki bir kök sebep, gerçek
#: bir kritik bulgunun üstüne çıkmıyor.
_ROOT_CAUSE_BOOST = 3.0

#: Bastırılmış bulgunun öncelik çarpanı. Sıfırlanmıyor ki açıldığında kendi içlerinde
#: anlamlı sıralansınlar.
_SUPPRESSED_DAMPING = 0.02


def _priority(
    draft: FindingDraft,
    change_state: str,
    open_days: int,
    acknowledged: bool,
    *,
    is_root_cause: bool = False,
    suppressed: bool = False,
) -> float:
    """Etki × aciliyet. Büyük olan üstte."""
    base = _SEVERITY_WEIGHT.get(draft.severity, 5.0)
    env = _ENVIRONMENT_WEIGHT.get((draft.environment or "prod").lower(), 1.0)
    change = _CHANGE_WEIGHT.get(change_state, 1.0)
    # Uzun süredir açık bir bulgu daha acildir, ama bu tek başına ciddiyeti geçemesin:
    # 30 günde en fazla %25 ek ağırlık.
    age = 1.0 + min(open_days, 30) * 0.0083
    score = base * env * change * age
    if is_root_cause:
        score *= _ROOT_CAUSE_BOOST
    if suppressed:
        score *= _SUPPRESSED_DAMPING
    if acknowledged:
        # Kabul edilenler ayrı bölümde; yine de kendi içlerinde sıralanabilsinler diye
        # tamamen sıfırlanmıyor.
        score *= 0.05
    return round(score, 3)


def _failed_section(builder, index: int) -> SectionResult:
    """Bölüm çökerse rapor tamamen düşmesin — o bölüm "bilinmiyor" olarak işaretlensin.
    Sessizce "sorunsuz" göstermek dürüstlük kuralına aykırı olurdu."""
    return SectionResult(
        key=getattr(builder, "section_key", f"section_{index}"),
        title=getattr(builder, "section_title", "Bölüm"),
        status="unknown",
        summary="Bu bölüm üretilirken hata oluştu.",
        unknown_reason="Bölüm üretimi hata verdi; ayrıntı sunucu loglarında.",
    )


def _build_plan(
    ctx: ReportContext, results: list[SectionResult]
) -> tuple[list[FindingDraft], dict[int, str], SuppressionPlan]:
    """Fingerprint'leri hesaplar, tekrarları eler ve bastırma planını üretir.

    Fingerprint hesabı TEK yerde: bağımlılık modülünün kendi hesabını yapması iki ayrı
    sonuç riski demek olurdu ve ayrışırlarsa bastırma sessizce yanlış bulguyu hedeflerdi.
    """
    seen: set[str] = set()
    fingerprints: dict[int, str] = {}
    unique: list[FindingDraft] = []
    for result in results:
        for draft in result.findings:
            fingerprint = make_fingerprint(draft.section, *draft.fingerprint_parts)
            if fingerprint in seen:
                continue  # aynı bulgu iki bölümden geldiyse bir kez raporla
            seen.add(fingerprint)
            fingerprints[id(draft)] = fingerprint
            unique.append(draft)
    plan = build_suppression_plan(unique, fingerprints, {i.id: i.group_id for i in ctx.instances})
    return unique, fingerprints, plan


async def _run_sections(ctx: ReportContext, report: HealthReport, session: AsyncSession) -> list[SectionResult]:
    builders = registered_sections()
    summary_builders = registered_summary_sections()
    total = len(builders) + len(summary_builders)
    results: list[SectionResult] = []

    for index, builder in enumerate(builders):
        try:
            result = await builder(ctx)
        except Exception:
            logger.exception("Rapor bölümü başarısız: %s", getattr(builder, "__name__", builder))
            result = _failed_section(builder, index)
        results.append(result)
        report.progress_pct = int((index + 1) * 100 / max(total, 1))
        report.progress_label = result.title
        await session.commit()

    # BASTIRMA PLANI BURADA ÜRETİLİYOR: özet bölümleri (yönetici özeti, değişenler) bulgu
    # SAYIYOR ve bastırılmışları saymamaları gerekiyor. Plan onlardan sonra üretilseydi
    # özet 40 kritik derken liste 1 kritik gösterirdi.
    ctx.unique_drafts, ctx.draft_fingerprints, ctx.suppression = _build_plan(ctx, results)

    summary_results: list[SectionResult] = []
    for offset, builder in enumerate(summary_builders):
        try:
            result = await builder(ctx, results)
        except Exception:
            logger.exception("Özet bölümü başarısız: %s", getattr(builder, "__name__", builder))
            result = _failed_section(builder, len(builders) + offset)
        summary_results.append(result)
        report.progress_pct = int((len(builders) + offset + 1) * 100 / max(total, 1))
        report.progress_label = result.title
        await session.commit()

    # Özetler sonra hesaplanır ama raporda önce görünür.
    return summary_results + results


# Öneri üretilemeyen kritik/uyarı bulguları için standart açıklama. Kural (Faz 17 İŞ 6):
# "her kritik/uyarı bulgusunun bir önerisi olsun; öneri veremiyorsa NEDENİNİ yazsın." Sessizce
# boş bırakmak, kullanıcıyı "bu bulguyla ne yapacağım?" sorusuyla baş başa bırakırdı.
NO_RECOMMENDATION_EXPLANATION = (
    "Bu bulgu için otomatik bir öneri üretilemedi: dbace'in elindeki veri sorunun nedenini "
    "belirlemeye yetmiyor. Bulgunun kanıt satırındaki metrikten yola çıkıp ilgili detay "
    "sayfasını inceleyin."
)


def _advice_from_legacy_fields(draft: FindingDraft) -> Advice:
    """Bölüm yapılandırılmış öneri vermediyse mevcut alanlardan standart yapıyı kurar.

    Faz 17 Ek İŞ B'nin şekli her yerde aynı olsun diye: bölümler kademeli olarak zengin
    öneriye geçebilir, arayüz bu arada iki farklı şekille uğraşmaz.
    """
    recommendation = (draft.recommendation or "").strip()
    if recommendation == NO_RECOMMENDATION_EXPLANATION:
        return unavailable(recommendation, title="Otomatik öneri üretilemedi")
    if not recommendation and not draft.commands:
        return unavailable(
            "Bu bulgu bilgi amaçlı; ayrı bir aksiyon gerektirmiyor.",
            title="Aksiyon gerekmiyor",
        )
    return simple_advice(
        title=recommendation or draft.title,
        why=draft.detail,
        commands=list(draft.commands or []),
    )


def _validated_drafts(results: list[SectionResult]) -> list[FindingDraft]:
    """Kalite kurallarını (Faz 17 İŞ 6) rapor kaydedilmeden ÖNCE uygular.

    * **Kanıt zorunluluğu** — kanıtsız bulgu rapora giremez, hata verir. Kanıt bulgunun
      doğruluğunun tek dayanağı; eksikse bulgunun kendisi güvenilmezdir.
    * **Aksiyon edilebilirlik** — kritik/uyarı bulgusunun önerisi yoksa bulgu ATILMAZ, yerine
      neden öneri verilemediği yazılır. Bulguyu atmak gerçek bir sorunu gizlemek olurdu;
      hata vermek de tek bir bölümün eksiği yüzünden tüm raporu düşürürdü.
    """
    drafts: list[FindingDraft] = []
    for result in results:
        for draft in result.findings:
            if not draft.evidence:
                raise ValueError(f"Kanıtsız bulgu üretildi: {result.key}/{draft.title}")
            if draft.severity in ("critical", "warning") and not (draft.recommendation or "").strip():
                logger.warning(
                    "Öneri üretilmeyen bulgu: %s/%s — standart açıklama eklendi",
                    result.key,
                    draft.title,
                )
                draft.recommendation = NO_RECOMMENDATION_EXPLANATION
            if draft.advice is None:
                draft.advice = _advice_from_legacy_fields(draft)
            drafts.append(draft)
    return drafts


async def generate_report(
    session: AsyncSession,
    scope: ReportScope,
    period_start: datetime,
    period_end: datetime,
    generated_by: str = "manual",
    report: HealthReport | None = None,
) -> HealthReport:
    """Raporu üretir ve kaydeder. `report` verilirse (arka plan akışı) o satır güncellenir."""
    started = datetime.now(UTC)

    if report is None:
        report = HealthReport(
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            scope_label=scope.label,
            period_start=period_start,
            period_end=period_end,
            generated_by=generated_by,
            status="running",
            progress_pct=0,
        )
        session.add(report)
        await session.commit()

    try:
        instances = await resolve_scope_instances(session, scope)
        previous = await _previous_report(session, scope, started)
        previous_findings: dict[str, ReportFinding] = {}
        if previous is not None:
            rows = (
                await session.execute(select(ReportFinding).where(ReportFinding.report_id == previous.id))
            ).scalars().all()
            previous_findings = {f.fingerprint: f for f in rows}

        ctx = ReportContext(
            session=session,
            scope=scope,
            instances=instances,
            period_start=period_start,
            period_end=period_end,
            previous=previous,
            previous_findings=previous_findings,
        )

        results = await _run_sections(ctx, report, session)
        drafts = _validated_drafts(results)

        # Durum makinesi (Ek İŞ A): kararlar, kapsam üyeliği ve otomatik geçişler.
        decisions = await load_decisions(session)
        membership = await build_scope_membership(session, instances)
        index = DecisionIndex(decisions, membership)

        # Plan `_run_sections` içinde üretildi (özet bölümleri de onu kullandı); burada
        # yeniden hesaplanmıyor.
        plan = ctx.suppression or SuppressionPlan()
        seen = set(ctx.draft_fingerprints.values())

        findings: list[ReportFinding] = []
        for draft in ctx.unique_drafts:
            fingerprint = ctx.draft_fingerprints[id(draft)]
            is_root_cause = plan.is_root(fingerprint)
            suppressed_by = plan.suppressed_by.get(fingerprint)

            prior = previous_findings.get(fingerprint)
            if prior is None:
                change_state = "new"
                open_days = 0
            else:
                open_days = prior.open_since_days + 1
                worsened = SEVERITY_ORDER.get(draft.severity, 0) > SEVERITY_ORDER.get(prior.severity, 0)
                change_state = "regressed" if worsened else "ongoing"

            finding_type = make_finding_type(draft.section, draft.fingerprint_parts)
            instance_id = draft.related_object_id if draft.related_object_type == "instance" else None
            decision = index.find(fingerprint, finding_type, instance_id)
            effective = resolve_status(decision, still_detected=True, now=started)

            if effective.auto_transition:
                from_status, to_status, note = effective.auto_transition
                await record_history(
                    session,
                    fingerprint=fingerprint,
                    finding_type=finding_type,
                    scope_type=decision.scope_type if decision else "instance",
                    scope_id=decision.scope_id if decision else instance_id,
                    from_status=from_status,
                    to_status=to_status,
                    note=note,
                    changed_by="sistem",
                )
                # Karar kaydı da güncellenir: aksi halde her raporda aynı otomatik geçiş
                # tekrar tekrar yazılır ve geçmiş gürültüye boğulur.
                if decision is not None:
                    decision.status = to_status
                    if to_status == STATUS_OPEN:
                        decision.expires_at = None

            payload = decision_payload(effective.decision)
            findings.append(
                ReportFinding(
                    report_id=report.id,
                    section=draft.section,
                    severity=draft.severity,
                    title=draft.title,
                    detail=draft.detail,
                    evidence=draft.evidence,
                    recommendation=draft.recommendation,
                    commands=draft.commands or None,
                    related_object_type=draft.related_object_type,
                    related_object_id=draft.related_object_id,
                    link_hint=draft.link_hint,
                    note=draft.note,
                    facts=draft.facts or None,
                    fingerprint=fingerprint,
                    finding_type=finding_type,
                    status=effective.status,
                    verification_failed=effective.verification_failed,
                    advice=advice_to_dict(draft.advice),
                    decision_note=payload["note"],
                    decision_reference=payload["reference"],
                    decision_until=payload["until"],
                    priority=_priority(
                        draft,
                        change_state,
                        open_days,
                        effective.status not in COUNTED_STATUSES,
                        is_root_cause=is_root_cause,
                        suppressed=suppressed_by is not None,
                    ),
                    is_root_cause=is_root_cause,
                    suppressed=suppressed_by is not None,
                    suppressed_by=suppressed_by,
                    open_since_days=open_days,
                    change_state=change_state,
                    # Geriye dönük uyumluluk: eski `acknowledged` bayrağı artık "açık değil"
                    # anlamına geliyor (arayüzün eski sürümleri ve sayaç sorguları için).
                    acknowledged=effective.status not in COUNTED_STATUSES,
                )
            )

        # Önceki raporda olup bu raporda olmayanlar: çözülmüş. Kayıt olarak tutuluyor ki
        # "yapılan işler" ve "dünden beri düzelenler" gerçek veriye dayansın.
        for fingerprint, prior in previous_findings.items():
            if fingerprint in seen or prior.change_state == "resolved":
                continue
            finding_type = prior.finding_type or ""
            decision = index.find(fingerprint, finding_type, prior.related_object_id)
            effective = resolve_status(decision, still_detected=False, now=started)
            if effective.auto_transition:
                from_status, to_status, note = effective.auto_transition
                await record_history(
                    session,
                    fingerprint=fingerprint,
                    finding_type=finding_type,
                    scope_type=decision.scope_type if decision else "instance",
                    scope_id=decision.scope_id if decision else prior.related_object_id,
                    from_status=from_status,
                    to_status=to_status,
                    note=note,
                    changed_by="sistem",
                )
                if decision is not None:
                    decision.status = to_status
            findings.append(
                ReportFinding(
                    report_id=report.id,
                    section=prior.section,
                    severity="ok",
                    title=prior.title,
                    detail="Bu bulgu bir önceki rapordan bu yana kapandı.",
                    evidence={
                        "previous_severity": prior.severity,
                        "previous_report_id": prior.report_id,
                        "open_since_days": prior.open_since_days,
                    },
                    fingerprint=fingerprint,
                    finding_type=finding_type,
                    status=STATUS_RESOLVED,
                    priority=_priority(
                        FindingDraft(
                            section=prior.section,
                            severity="ok",
                            title=prior.title,
                            detail="",
                            evidence={"x": 1},
                            fingerprint_parts=(),
                        ),
                        "resolved",
                        prior.open_since_days,
                        False,
                    ),
                    open_since_days=prior.open_since_days,
                    change_state="resolved",
                    acknowledged=False,
                    related_object_type=prior.related_object_type,
                    related_object_id=prior.related_object_id,
                )
            )

        for finding in findings:
            session.add(finding)

        # Genel durum: KABUL EDİLMEMİŞ bulguların en kötüsü. Kabul edilmiş bir kritik bulgu
        # raporun tamamını kırmızıya boyamamalı (talep edilen "kritik sayısını şişirmesin").
        # Kritik/uyarı sayaçları ve genel durum YALNIZCA "açık" bulguları sayar (Ek İŞ A):
        # yoksayılan, ertelenen, planlanan, risk kabul edilen ya da kapanan bulgular raporun
        # tamamını kırmızıya boyamaz.
        # BASTIRILMIŞ BULGULAR SAYILMIYOR (Faz 28 İŞ 2): bir düğüm düştüğünde kritik sayacı
        # 40 gösterirse "kritik" kelimesi anlamını yitirir. Bulgular silinmiyor, yalnızca
        # sayaca ve genel duruma girmiyorlar.
        counted = [f for f in findings if f.status in COUNTED_STATUSES and not f.suppressed]
        worst = max((SEVERITY_ORDER.get(f.severity, 0) for f in counted), default=0)
        report.overall_status = {3: "critical", 2: "warning", 1: "info", 0: "ok"}[worst]
        # "Kök sebep nedeniyle N kontrol yapılamadı" satırları. Bastırılan bulgular
        # KAYBOLMUYOR: raporda duruyor, işaretli ve bu özetin altından açılabiliyor.
        report.suppression = {
            "roots": plan.summary_rows(),
            "suppressed_total": sum(plan.counts.values()),
        }
        report.sections = {
            "order": [r.key for r in results],
            "items": {
                r.key: {
                    "title": r.title,
                    "status": r.status,
                    "summary": r.summary,
                    "data": r.data,
                    "unknown_reason": r.unknown_reason,
                }
                for r in results
            },
        }
        report.previous_report_id = previous.id if previous else None
        report.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        report.status = "done"
        report.progress_pct = 100
        report.progress_label = None
        await session.commit()
        return report
    except Exception as exc:  # noqa: BLE001 — hata raporun kendisine yazılır, sessizce kaybolmaz
        logger.exception("Rapor üretimi başarısız (scope=%s/%s)", scope.scope_type, scope.scope_id)
        report.status = "failed"
        report.error = str(exc)[:2000]
        report.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        await session.commit()
        raise


async def enqueue_report(
    scope: ReportScope, period_start: datetime, period_end: datetime, generated_by: str = "manual"
) -> HealthReport:
    """Raporu arka planda üretir; çağıran hemen "queued" satırını alır.

    Kendi oturumunu açan ayrı bir task kullanılıyor — rapor üretimi saniyeler sürebilir ve
    istek/döngü oturumunu o süre boyunca meşgul etmemeli (toplama döngüsünü bloke etmesin).
    """
    async with SessionLocal() as session:
        label = await resolve_scope_label(session, scope.scope_type, scope.scope_id)
        scope = ReportScope(scope.scope_type, scope.scope_id, label)
        report = HealthReport(
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            scope_label=label,
            period_start=period_start,
            period_end=period_end,
            generated_by=generated_by,
            status="queued",
            progress_pct=0,
            progress_label="Sıraya alındı",
        )
        session.add(report)
        await session.commit()
        report_id = report.id

    async def _run() -> None:
        async with SessionLocal() as bg_session:
            row = await bg_session.get(HealthReport, report_id)
            if row is None:
                return
            row.status = "running"
            await bg_session.commit()
            try:
                await generate_report(bg_session, scope, period_start, period_end, generated_by, report=row)
            except Exception:
                # generate_report zaten status="failed" yazdı ve logladı.
                pass

    asyncio.create_task(_run())

    async with SessionLocal() as session:
        return await session.get(HealthReport, report_id)


def default_period(days: int = 1, end: datetime | None = None) -> tuple[datetime, datetime]:
    period_end = end or datetime.now(UTC)
    return period_end - timedelta(days=days), period_end


async def scheduled_scopes(session: AsyncSession, scope_mode: str) -> list[ReportScope]:
    """Zamanlanmış üretimin kapsayacağı kapsamlar.

    "customers" modunda her müşteri için ayrı rapor üretilir — yönetici raporu doğası gereği
    müşteri bazlıdır ("X Bank'ın sistemleri sağlıklı mı?"), tüm sistemi tek raporda özetlemek
    çok müşterili kurulumda anlamsız olurdu.
    """
    scopes: list[ReportScope] = []
    if scope_mode in ("global", "both"):
        scopes.append(ReportScope("global", None, "Tüm sistem"))
    if scope_mode in ("customers", "both"):
        customers = (await session.execute(select(Customer).order_by(Customer.name))).scalars().all()
        scopes.extend(ReportScope("customer", c.id, c.name) for c in customers)
    return scopes


async def run_scheduled_reports() -> int:
    """Günlük zamanlanmış rapor üretimi. Kaç rapor üretildiğini döndürür.

    Kapsamlar SIRAYLA üretilir (paralel değil): rapor üretimi veritabanı okuması yoğun ve
    aynı anda onlarca kapsam çalıştırmak toplama döngüsüyle yarışırdı. Bir kapsam hata alırsa
    diğerleri devam eder.
    """
    from app.services.settings import get_health_report_schedule

    async with SessionLocal() as session:
        schedule = await get_health_report_schedule(session)
        if not schedule["enabled"]:
            logger.info("Zamanlanmış sağlık raporu kapalı, atlanıyor")
            return 0
        scopes = await scheduled_scopes(session, schedule["scope_mode"])

    period_start, period_end = default_period(days=1)
    produced = 0
    for scope in scopes:
        async with SessionLocal() as session:
            try:
                await generate_report(session, scope, period_start, period_end, generated_by="schedule")
                produced += 1
            except Exception:
                logger.exception("Zamanlanmış rapor başarısız: %s/%s", scope.scope_type, scope.scope_id)
    return produced
