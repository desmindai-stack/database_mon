"""Öneri ve çözüm adımları standardı (Faz 17 Ek İŞ B).

dbace'te öneriler dört ayrı yerde üretiliyordu ve her biri kendi şeklindeydi: rapor bulguları
düz metin + komut listesi, dashboard kartları başlık + adımlar + tek komut, DPA index önerisi
gerekçe + DDL, tahminler ise adım adım playbook. Aynı ürünün dört farklı "öneri" kavramı
olması hem kullanıcı için kafa karıştırıcıydı hem de her yerde ayrı ayrı eksik kalıyordu
(kiminde doğrulama sorgusu vardı kiminde yoktu).

Bu modül TEK bir yapı tanımlıyor; dört üretici de buna dönüştürülüyor ve arayüzde tek bir
bileşenle gösteriliyor:

    Öneri: <kısa eylem>            → title
    Neden                          → why (iş etkisi dahil, "yapılmazsa ne olur")
    Adımlar (numaralı)             → steps[].action
      └ komut                      → steps[].command (tam, çalıştırılabilir, kopyalanabilir)
    Dikkat                         → cautions[] (kilitleme, süre, bakım penceresi)
    Tahmini süre / Geri alma       → estimated_duration, rollback
    Doğrulama                      → verification (uygulandıktan sonra "düzeldi mi" sorgusu)

Öneri üretilemiyorsa `unavailable_reason` doldurulur ve yapı yine döner — boş bırakmak yerine
NEDEN üretilemediği yazılır (Faz 17 İŞ 6 aksiyon edilebilirlik kuralının devamı).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdviceStep:
    """Tek bir eylem. `command` varsa arayüzde ayrı satırda, kopyalanabilir kutuda gösterilir."""

    action: str
    command: str | None = None


@dataclass
class Advice:
    # "Öneri: " ön eki gösterim katmanında ekleniyor — başlığın kendisi kısa bir EYLEM olmalı
    # ("work_mem değerini artırın"), durum tespiti değil ("work_mem düşük").
    title: str
    why: str = ""
    steps: list[AdviceStep] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    estimated_duration: str | None = None
    rollback: str | None = None
    verification: str | None = None
    unavailable_reason: str | None = None

    @property
    def is_actionable(self) -> bool:
        return self.unavailable_reason is None and bool(self.steps or self.title)


def unavailable(reason: str, title: str = "Öneri üretilemedi") -> Advice:
    """Öneri verilemediğinde NEDENİNİ taşıyan yapı — boş bırakmak yasak."""
    return Advice(title=title, unavailable_reason=reason)


def advice_to_dict(advice: Advice | None) -> dict[str, Any] | None:
    if advice is None:
        return None
    return {
        "title": advice.title,
        "why": advice.why,
        "steps": [{"action": s.action, "command": s.command} for s in advice.steps],
        "cautions": list(advice.cautions),
        "estimated_duration": advice.estimated_duration,
        "rollback": advice.rollback,
        "verification": advice.verification,
        "unavailable_reason": advice.unavailable_reason,
    }


def advice_from_playbook(
    title: str,
    why: str,
    playbook: list[dict[str, Any]] | None,
    *,
    verification: str | None = None,
    cautions: list[str] | None = None,
    estimated_duration: str | None = None,
    rollback: str | None = None,
) -> Advice:
    """Tahminlerin adım adım planını (services/prediction_playbooks.py) standart yapıya çevirir.

    Playbook adımları zaten {title, detail, command} — burada `action` olarak başlık + detay
    birleştiriliyor; komut olduğu gibi taşınıyor.
    """
    steps = [
        AdviceStep(
            action=f"{step.get('title', '')}: {step.get('detail', '')}".strip(": ").strip(),
            command=step.get("command"),
        )
        for step in (playbook or [])
    ]
    return Advice(
        title=title,
        why=why,
        steps=steps,
        cautions=list(cautions or []),
        estimated_duration=estimated_duration,
        rollback=rollback,
        verification=verification,
    )


def simple_advice(
    title: str,
    why: str,
    commands: list[str] | None = None,
    *,
    step_action: str = "Aşağıdaki komutu çalıştırın.",
    verification: str | None = None,
    cautions: list[str] | None = None,
) -> Advice:
    """Tek eylemli öneriler için kısa yol (dashboard kartları, ön koşul düzeltmeleri).

    Komut listesi verilirse her komut ayrı bir adım olur; hiç komut yoksa öneri yine yapıya
    uyar (başlık + neden), sadece adım listesi boş kalır.
    """
    steps = [AdviceStep(action=step_action, command=command) for command in (commands or [])]
    return Advice(
        title=title,
        why=why,
        steps=steps,
        cautions=list(cautions or []),
        verification=verification,
    )
