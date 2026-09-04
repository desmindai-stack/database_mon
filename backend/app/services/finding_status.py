"""Bulgu durum makinesi (Faz 17 Ek İŞ A).

Tek bir "kabul edildi" bayrağı, gerçek bir DBA akışını taşıyamıyordu: "şimdi bakamıyorum,
salıya ertele", "bu riski bilerek kabul ediyoruz", "değişiklik talebi açtık", "düzelttik ama
doğrulanması lazım" birbirinden çok farklı kararlar. Bu modül o kararları bir durum makinesine
bağlıyor.

Durumlar:

    open                            Varsayılan. Raporun ana bölümlerinde görünür, sayaçlara girer.
    ignored                         Bilinçli olarak susturuldu (ör. bu ortamda geçersiz).
    deferred                        Ertelendi; `expires_at` gelince OTOMATİK olarak open'a döner.
    risk_accepted                   Risk bilinçli kabul edildi (düzeltilmeyecek).
    planned                         Bir iş kaydına bağlandı; `reference` alanı ticket/CR no taşır.
    resolved_pending_verification   Düzeltildi denildi, bir sonraki raporda doğrulanacak.
    resolved                        Bulgu artık tespit edilmiyor.

Otomatik geçişler (rapor üretimi sırasında, `apply_automatic_transitions`):

* `deferred` + süresi doldu → `open`
* `resolved_pending_verification` + bulgu HÂLÂ tespit ediliyor → `open` +
  `verification_failed` işareti. Bu, yanlış kapatmaları yakalayan asıl mekanizma.
* Bulgu artık tespit edilmiyor → `resolved` ("düzelenler" bölümüne düşer).

Otomatik geçişler de geçmişe yazılır (`changed_by="sistem"`) — "bunu kim açtı?" sorusu her
zaman cevaplanabilir olmalı.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    DatabaseGroup,
    FindingAcknowledgement,
    FindingStatusHistory,
    Instance,
)

logger = logging.getLogger(__name__)

STATUS_OPEN = "open"
STATUS_IGNORED = "ignored"
STATUS_DEFERRED = "deferred"
STATUS_RISK_ACCEPTED = "risk_accepted"
STATUS_PLANNED = "planned"
STATUS_RESOLVED_PENDING = "resolved_pending_verification"
STATUS_RESOLVED = "resolved"

ALL_STATUSES = (
    STATUS_OPEN,
    STATUS_IGNORED,
    STATUS_DEFERRED,
    STATUS_RISK_ACCEPTED,
    STATUS_PLANNED,
    STATUS_RESOLVED_PENDING,
    STATUS_RESOLVED,
)

STATUS_LABELS_TR = {
    STATUS_OPEN: "Açık",
    STATUS_IGNORED: "Yoksayıldı",
    STATUS_DEFERRED: "Ertelendi",
    STATUS_RISK_ACCEPTED: "Risk kabul",
    STATUS_PLANNED: "Planlandı",
    STATUS_RESOLVED_PENDING: "Çözüldü (doğrulanacak)",
    STATUS_RESOLVED: "Çözüldü",
}

# Raporun ANA bölümlerinde görünen durumlar. Diğerleri "Bilinen konular"a düşer.
MAIN_SECTION_STATUSES = frozenset({STATUS_OPEN, STATUS_RESOLVED_PENDING})
# Kritik/uyarı sayaçlarına giren durumlar — yalnızca gerçekten açık olanlar.
COUNTED_STATUSES = frozenset({STATUS_OPEN})
# Yönetici raporunda ayrı bir bölümde gösterilenler (biri ekibin çalıştığını, diğeri bilinçli
# kararı gösterir); "ignored" oraya HİÇ girmez.
EXECUTIVE_VISIBLE_DECISIONS = (STATUS_PLANNED, STATUS_RISK_ACCEPTED)

SCOPE_LEVELS = ("instance", "group", "application", "customer", "global")

# Süre girilebilen durumlar — diğerlerinde tarih anlamsız.
STATUSES_WITH_DEADLINE = frozenset({STATUS_DEFERRED, STATUS_IGNORED})


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class ScopeMembership:
    """instance_id → ait olduğu grup / uygulama / müşteri kimlikleri.

    Grup/uygulama/müşteri kapsamlı bir kararın hangi bulgulara uygulanacağını belirlemek için
    gerekli; rapor üretiminde bir kez hesaplanıp tüm bulgular için kullanılır.
    """

    group_of: dict[int, int | None]
    application_of: dict[int, int | None]
    customer_of: dict[int, int | None]

    def matches(self, instance_id: int | None, scope_type: str, scope_id: int | None) -> bool:
        if scope_type == "global":
            return True
        if instance_id is None:
            return False
        if scope_type == "instance":
            return instance_id == scope_id
        if scope_type == "group":
            return self.group_of.get(instance_id) == scope_id
        if scope_type == "application":
            return self.application_of.get(instance_id) == scope_id
        if scope_type == "customer":
            return self.customer_of.get(instance_id) == scope_id
        return False


async def build_scope_membership(session: AsyncSession, instances: list[Instance]) -> ScopeMembership:
    group_ids = sorted({i.group_id for i in instances if i.group_id})
    groups: dict[int, DatabaseGroup] = {}
    if group_ids:
        rows = (await session.execute(select(DatabaseGroup).where(DatabaseGroup.id.in_(group_ids)))).scalars().all()
        groups = {g.id: g for g in rows}

    application_ids = sorted({g.application_id for g in groups.values()})
    applications: dict[int, Application] = {}
    if application_ids:
        rows = (
            await session.execute(select(Application).where(Application.id.in_(application_ids)))
        ).scalars().all()
        applications = {a.id: a for a in rows}

    group_of: dict[int, int | None] = {}
    application_of: dict[int, int | None] = {}
    customer_of: dict[int, int | None] = {}
    for instance in instances:
        group = groups.get(instance.group_id) if instance.group_id else None
        application = applications.get(group.application_id) if group else None
        group_of[instance.id] = group.id if group else None
        application_of[instance.id] = application.id if application else None
        customer_of[instance.id] = application.customer_id if application else None
    return ScopeMembership(group_of, application_of, customer_of)


def make_finding_type(section: str, fingerprint_parts: tuple[str, ...]) -> str:
    """"<bölüm>:<bulgu tipi>" — hedef nesne kimliği İÇERMEZ.

    fingerprint_parts'ın ilk elemanı bölümlerde tutarlı biçimde bulgu tipini taşıyor
    ("collection_gap", "leader_change", "slow_query", ...); sonraki elemanlar hedef nesne
    kimlikleri. Tip anahtarı yalnızca ilkini alır, böylece "bu bulgu tipini her yerde yoksay"
    kararı farklı sunuculardaki aynı tip bulguları da kapsar.
    """
    head = fingerprint_parts[0] if fingerprint_parts else ""
    return f"{section}:{head}"


def _decision_rank(decision: FindingAcknowledgement) -> tuple[int, datetime]:
    """Birden fazla karar eşleşirse hangisi geçerli.

    En DAR kapsam kazanır: bir sunucu için verilmiş özel karar, aynı tip için verilmiş küresel
    kararı ezer (istisna yönetimi böyle çalışmalı). Eşit kapsamda en yeni karar geçerli.
    """
    order = {"instance": 0, "group": 1, "application": 2, "customer": 3, "global": 4}
    return (order.get(decision.scope_type, 9), -(as_utc(decision.acknowledged_at) or datetime.min.replace(tzinfo=UTC)).timestamp())


@dataclass
class EffectiveStatus:
    status: str
    decision: FindingAcknowledgement | None = None
    verification_failed: bool = False
    # Otomatik geçiş olduysa geçmişe yazılacak kayıt bilgisi.
    auto_transition: tuple[str, str, str] | None = None  # (from, to, note)


class DecisionIndex:
    """Kararları bulgularla eşleştirir."""

    def __init__(self, decisions: list[FindingAcknowledgement], membership: ScopeMembership):
        self._by_fingerprint: dict[str, list[FindingAcknowledgement]] = {}
        self._by_type: list[FindingAcknowledgement] = []
        self._membership = membership
        for decision in decisions:
            if decision.scope_type == "instance":
                self._by_fingerprint.setdefault(decision.fingerprint, []).append(decision)
            else:
                self._by_type.append(decision)

    def find(self, fingerprint: str, finding_type: str, instance_id: int | None) -> FindingAcknowledgement | None:
        candidates = list(self._by_fingerprint.get(fingerprint, []))
        for decision in self._by_type:
            if decision.finding_type != finding_type:
                continue
            if self._membership.matches(instance_id, decision.scope_type, decision.scope_id):
                candidates.append(decision)
        if not candidates:
            return None
        candidates.sort(key=_decision_rank)
        return candidates[0]


def resolve_status(
    decision: FindingAcknowledgement | None, *, still_detected: bool, now: datetime
) -> EffectiveStatus:
    """Bir bulgunun bu rapordaki etkin durumu + gerekiyorsa otomatik geçiş.

    `still_detected=False` yalnızca önceki raporda olup bu raporda kaybolan bulgular için
    çağrılır.
    """
    if not still_detected:
        # Bulgu artık tespit edilmiyor → çözüldü. Karar ne olursa olsun (ertelenmiş bir bulgu
        # da kendiliğinden düzelmiş olabilir).
        previous = decision.status if decision else STATUS_OPEN
        auto = None if previous == STATUS_RESOLVED else (previous, STATUS_RESOLVED, "Bulgu artık tespit edilmiyor.")
        return EffectiveStatus(STATUS_RESOLVED, decision, auto_transition=auto)

    if decision is None:
        return EffectiveStatus(STATUS_OPEN)

    status = decision.status

    if status == STATUS_RESOLVED_PENDING:
        # Asıl koruma: "düzelttim" denmişti ama bulgu hâlâ duruyor.
        return EffectiveStatus(
            STATUS_OPEN,
            decision,
            verification_failed=True,
            auto_transition=(
                STATUS_RESOLVED_PENDING,
                STATUS_OPEN,
                "Çözüm doğrulanamadı: bulgu bu raporda hâlâ tespit ediliyor.",
            ),
        )

    if status in STATUSES_WITH_DEADLINE:
        expires = as_utc(decision.expires_at)
        if expires is not None and expires <= now:
            return EffectiveStatus(
                STATUS_OPEN,
                decision,
                auto_transition=(status, STATUS_OPEN, "Süre doldu; bulgu tekrar açıldı."),
            )

    if status == STATUS_RESOLVED:
        # Kullanıcı "çözüldü" demiş ama bulgu hâlâ tespit ediliyor — doğrulama başarısız
        # sayılır. resolved yalnızca bulgunun kaybolmasıyla kalıcı olabilir.
        return EffectiveStatus(
            STATUS_OPEN,
            decision,
            verification_failed=True,
            auto_transition=(STATUS_RESOLVED, STATUS_OPEN, "Çözüldü işaretlenmişti ama bulgu hâlâ tespit ediliyor."),
        )

    return EffectiveStatus(status, decision)


async def record_history(
    session: AsyncSession,
    *,
    fingerprint: str,
    finding_type: str | None,
    scope_type: str,
    scope_id: int | None,
    from_status: str | None,
    to_status: str,
    note: str | None,
    changed_by: str,
    reference: str | None = None,
    expires_at: datetime | None = None,
) -> FindingStatusHistory:
    entry = FindingStatusHistory(
        fingerprint=fingerprint,
        finding_type=finding_type,
        scope_type=scope_type,
        scope_id=scope_id,
        from_status=from_status,
        to_status=to_status,
        note=note,
        reference=reference,
        expires_at=expires_at,
        changed_by=changed_by,
    )
    session.add(entry)
    return entry


async def apply_decision(
    session: AsyncSession,
    *,
    fingerprint: str,
    finding_type: str,
    scope_type: str,
    scope_id: int | None,
    status: str,
    note: str,
    changed_by: str,
    reference: str | None = None,
    expires_at: datetime | None = None,
) -> FindingAcknowledgement | None:
    """Durum kararını kaydeder ve geçmişe yazar.

    `status="open"` özel: var olan kararı SİLER (bulguyu normale döndürmenin yolu bu) ve
    geçmişe bir "açıldı" satırı yazar.
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"Bilinmeyen durum: {status}")
    if scope_type not in SCOPE_LEVELS:
        raise ValueError(f"Bilinmeyen kapsam: {scope_type}")
    if not (note or "").strip():
        # Not zorunlu: notsuz bir susturma kaydı altı ay sonra "bunu neden kapattık?" sorusunu
        # cevapsız bırakır.
        raise ValueError("Durum değişikliği için not zorunlu")
    if scope_type != "global" and scope_id is None:
        raise ValueError("Bu kapsam için hedef kimliği (scope_id) zorunlu")

    existing = (
        await session.execute(
            select(FindingAcknowledgement).where(
                FindingAcknowledgement.fingerprint == fingerprint,
                FindingAcknowledgement.scope_type == scope_type,
                FindingAcknowledgement.scope_id.is_(None)
                if scope_id is None
                else FindingAcknowledgement.scope_id == scope_id,
            )
        )
    ).scalar_one_or_none()
    previous_status = existing.status if existing else STATUS_OPEN

    await record_history(
        session,
        fingerprint=fingerprint,
        finding_type=finding_type,
        scope_type=scope_type,
        scope_id=scope_id,
        from_status=previous_status,
        to_status=status,
        note=note,
        changed_by=changed_by,
        reference=reference,
        expires_at=expires_at,
    )

    if status == STATUS_OPEN:
        if existing is not None:
            await session.delete(existing)
        await session.commit()
        return None

    if existing is None:
        existing = FindingAcknowledgement(
            fingerprint=fingerprint, scope_type=scope_type, scope_id=scope_id
        )
        session.add(existing)
    existing.finding_type = finding_type
    existing.status = status
    existing.acknowledged_by = changed_by
    existing.acknowledged_at = datetime.now(UTC)
    existing.expires_at = expires_at if status in STATUSES_WITH_DEADLINE else None
    existing.reference = reference if status == STATUS_PLANNED else existing.reference
    existing.note = note
    await session.commit()
    await session.refresh(existing)
    return existing


async def load_decisions(session: AsyncSession) -> list[FindingAcknowledgement]:
    return list((await session.execute(select(FindingAcknowledgement))).scalars().all())


def decision_payload(decision: FindingAcknowledgement | None) -> dict[str, Any]:
    if decision is None:
        return {"note": None, "reference": None, "until": None}
    return {
        "note": decision.note,
        "reference": decision.reference,
        "until": as_utc(decision.expires_at),
    }
