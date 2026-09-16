"""Bir sorgunun planı NEREDEN alınabilir — öncelik sırası ve her kaynağın durumu (Faz 31 İŞ 2).

EXPLAIN ANALYZE, pg_stat_statements'ın normalize metninde ($1) gerçek değer olmadığı için
reddediliyordu. Gerekçe doğru, ama dbace'in elinde iki gerçek kaynak zaten vardı ve
kullanılmıyordu. Öncelik sırası güvenilirliğe göre:

1. **auto_explain planı** — gerçek çalıştırmanın KENDİ planı. Varsa EXPLAIN hiç çalıştırılmaz.
2. **Gerçek değerli örnekle EXPLAIN ANALYZE** — değerler gerçek bir çalıştırmadan
   (bekleme örnekleyicisi, pg_stat_activity) ama sorgu YENİDEN çalıştırılıyor. Kullanıcı
   onaylar; yalnızca admin.
3. **Değerden bağımsız plan** — sorgu çalıştırılmıyor; parametre değerine göre gerçek plan
   farklı olabilir.
4. **Hiçbiri** — neden.

DÖRT SEÇENEK DE HER ZAMAN DÖNÜYOR, kullanılamayanlar sebebiyle. Kullanılamayan bir kaynağı
gizlemek kullanıcıya neyin eksik olduğunu (ve nasıl açılacağını) söylememek olurdu.

## Gerçek değerli örneğin ÖLÇÜLEN sınırı

Bind parametreli sürücüler (JDBC PreparedStatement, asyncpg, çoğu ORM) pg_stat_activity'de
`$1` gösteriyor — değer görünmüyor (PG 17.11 ve 15.19'da ölçüldü). Örnek yalnızca değerleri
metne gömen uygulamalar için var. Sebep metni bu ayrımı yapıyor.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CapturedPlan, Instance, SlowQuerySample, WaitQuerySignature
from app.services.analysis_settings import get_analysis_settings
from app.services.auto_explain import MANAGED_SERVICE_GUIDANCE, plan_source_caveat, plan_source_label
from app.services.explain_service import ANALYZE_STATEMENT_TIMEOUT_MS, validate_analyzable, validate_explainable
from app.services.generic_plan import has_placeholders
from app.services.plan_capture import fingerprint
from app.services.sql_analysis import (
    PG_VERSION_GENERIC_PLAN,
    PG_VERSION_PLAN_CACHE_MODE,
    detect_truncation,
)

KIND_CAPTURED = "captured"
KIND_SAMPLE = "sample_analyze"
KIND_GENERIC = "generic"
KIND_UNAVAILABLE = "unavailable"


def captured_unavailable_reason(instance: Instance) -> str:
    """auto_explain planı neden yok — `captured-plans` listesi ile plan kaynakları AYNI metni
    kullanıyor (iki ayrı açıklama zamanla ayrışırdı)."""
    if instance.engine != "postgresql":
        return (
            "auto_explain yalnızca PostgreSQL'de var; bu motor için gerçek çalıştırma planı "
            "yakalanamıyor."
        )
    if not (instance.options or {}).get("agent_url"):
        return (
            "Bu veritabanı için host-agent yapılandırılmamış. auto_explain planları sunucu "
            "log'undan okunuyor ve log'a erişim agent üzerinden sağlanıyor."
        )
    return (
        "Bu sorgu için henüz yakalanmış plan yok. Ön koşullar panelindeki auto_explain "
        "kontrollerine bakın: kütüphane yüklü, eşik ayarlı ve log_format='json' olmalı. Üçü de "
        "tamamsa, sorgu eşiği aşan bir sürede çalışana kadar plan birikmez."
    )


def _option(kind: str, available: bool, *, label: str, reason: str | None = None,
            caveat: str | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "available": available,
        "label": label,
        "reason": None if available else reason,
        "caveat": caveat if available else None,
        "detail": detail or {},
    }


async def resolve_plan_sources(session: AsyncSession, instance: Instance, sample: SlowQuerySample) -> dict[str, Any]:
    settings = await get_analysis_settings(session)
    query = sample.query or ""
    queryid = sample.queryid
    options = [
        await _captured_option(session, instance, query, queryid),
        await _sample_option(session, instance, queryid, store_enabled=settings["store_real_query_samples"]),
        _generic_option(instance, query),
    ]
    available = [o for o in options if o["available"]]
    # Dördüncü seçenek "hiçbiri": yalnızca diğer üçü de kullanılamıyorsa geçerli ve sebebi
    # onların sebeplerinin toplamı.
    none_reason = " ".join(f"{o['label']}: {o['reason']}" for o in options if o["reason"])
    options.append({
        "kind": KIND_UNAVAILABLE,
        "available": not available,
        "label": "Plan alınamıyor",
        "reason": none_reason if not available else None,
        "caveat": None,
        "detail": {},
    })
    return {
        "sample_id": sample.id,
        "queryid": queryid,
        "recommended": available[0]["kind"] if available else KIND_UNAVAILABLE,
        "options": options,
    }


async def _captured_option(session, instance, query, queryid) -> dict[str, Any]:
    conditions = [CapturedPlan.query_fingerprint == fingerprint(query)]
    if queryid:
        conditions.append(CapturedPlan.queryid == queryid)
    plan = (
        await session.execute(
            select(CapturedPlan)
            .where(CapturedPlan.instance_id == instance.id, or_(*conditions))
            .order_by(CapturedPlan.captured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    label = plan_source_label("auto_explain")
    if plan is None:
        detail = {}
        if instance.engine == "postgresql" and not (instance.options or {}).get("agent_url"):
            detail["managed_service_guidance"] = MANAGED_SERVICE_GUIDANCE
        return _option(KIND_CAPTURED, False, label=label, reason=captured_unavailable_reason(instance), detail=detail)
    caveat = plan_source_caveat("auto_explain")
    if not plan.has_actual_rows:
        caveat = (
            "auto_explain.log_analyze kapalı olduğu için GERÇEK SATIR SAYISI yok — plan gerçek "
            "çalıştırmanın planı ama tahmini/gerçek sapma analizi yapılamaz."
        )
    return _option(
        KIND_CAPTURED, True, label=label, caveat=caveat,
        detail={"plan_id": plan.id, "captured_at": plan.captured_at.isoformat() if plan.captured_at else None,
                "duration_ms": plan.duration_ms, "has_actual_rows": plan.has_actual_rows},
    )


async def _sample_option(session, instance, queryid, *, store_enabled: bool) -> dict[str, Any]:
    label = plan_source_label(KIND_SAMPLE)
    if instance.engine != "postgresql":
        return _option(KIND_SAMPLE, False, label=label, reason="Yalnızca PostgreSQL için.")
    if not store_enabled:
        return _option(
            KIND_SAMPLE, False, label=label,
            reason=(
                "Gerçek değerli sorgu örneği saklama KAPALI (varsayılan). Örnek metin uygulamanın "
                "gömdüğü değerleri (kimlik no, e-posta, tutar) içerebildiği için bilinçli olarak "
                "kapalı geliyor. Yönetim → Ayarlar → Analiz bölümünden açılabilir."
            ),
        )
    if not queryid:
        return _option(
            KIND_SAMPLE, False, label=label,
            reason=(
                "Sorgunun kimliği (queryid) yok: PostgreSQL 14 öncesinde pg_stat_activity query_id "
                "taşımıyor ya da compute_query_id kapalı. Örnekleyici bu sorguyu eşleştiremiyor."
            ),
        )
    signature = (
        await session.execute(
            select(WaitQuerySignature).where(
                WaitQuerySignature.instance_id == instance.id, WaitQuerySignature.queryid == queryid
            )
        )
    ).scalar_one_or_none()
    if signature is None:
        return _option(
            KIND_SAMPLE, False, label=label,
            reason=(
                "Bekleme örnekleyicisi bu sorguyu çalışırken hiç görmedi (örnekleme saniyede bir; "
                "çok kısa süren sorgular yakalanmayabilir)."
            ),
        )
    if not signature.sample_query_text:
        if signature.seen_bind_parameters:
            reason = (
                "Örnekleyici bu sorguyu yalnızca PARAMETRELİ hâliyle ($1) gördü: uygulama bind "
                "parametresi kullanıyor (JDBC PreparedStatement, çoğu ORM ve sürücü) ve PostgreSQL "
                "değerleri pg_stat_activity'de göstermiyor. Bu sorgu için gerçek değerli örnek "
                "oluşamaz; auto_explain tek gerçek plan kaynağı."
            )
        else:
            reason = (
                "Henüz saklanmış örnek yok: saklama açıldıktan sonra sorgu örnekleyicinin çalıştığı "
                "bir anda görülmedi, ya da görülen metin kesikti (track_activity_query_size)."
            )
        return _option(KIND_SAMPLE, False, label=label, reason=reason)
    try:
        validate_analyzable(signature.sample_query_text)
    except ValueError as exc:
        return _option(KIND_SAMPLE, False, label=label, reason=f"Örnek çalıştırılamaz: {exc}")
    duration = signature.sample_duration_ms
    version = instance.server_version_num
    # Ölçüldü: PG 15 yardımcı ifadelerin (EXPLAIN) sabitlerini pg_stat_statements'ta normalize
    # ETMİYOR; 16+ ediyor. Sürüm bilinmiyorsa en kötü durum varsayılıyor.
    leaves_values = version is None or version < 160_000
    leak_warning = (
        " DİKKAT: bu sunucu PostgreSQL 16'dan eski (ya da sürüm bilinmiyor) — çalıştırılan EXPLAIN "
        "ifadesi, içindeki gerçek değerlerle birlikte sunucunun kendi pg_stat_statements "
        "görünümünde kalır (16 ve sonrası değerleri normalize eder). dbace kendi tarafında bu "
        "metni değerlerden arındırarak saklar."
        if leaves_values
        else ""
    )
    return _option(
        KIND_SAMPLE, True, label=label,
        caveat=(
            "Sorgu izlenen sunucuda GERÇEKTEN çalıştırılacak"
            + (f"; örneklenen çalıştırma en az {duration:.0f} ms sürüyordu" if duration else "")
            + f". Salt-okunur işlemde, {ANALYZE_STATEMENT_TIMEOUT_MS // 1000} sn zaman aşımıyla "
            "çalıştırılır ve geri alınır. " + (plan_source_caveat(KIND_SAMPLE) or "") + leak_warning
        ),
        # ÖRNEK METİN BURADA YOK: bu uç viewer'a da açık. Metin yalnızca admin'in POST ettiği
        # EXPLAIN yanıtında (planın sorgusu olarak) görünüyor.
        detail={
            "captured_at": signature.sample_captured_at.isoformat() if signature.sample_captured_at else None,
            "duration_ms": duration,
            "timeout_ms": ANALYZE_STATEMENT_TIMEOUT_MS,
            "requires_admin": True,
            "leaves_values_in_server_statistics": leaves_values,
        },
    )


def _generic_option(instance: Instance, query: str) -> dict[str, Any]:
    label = plan_source_label(KIND_GENERIC)
    try:
        validate_explainable(query)
    except ValueError as exc:
        return _option(KIND_GENERIC, False, label=label, reason=str(exc))
    truncation = detect_truncation(query)
    if truncation.truncated:
        return _option(KIND_GENERIC, False, label=label, reason="Sorgu metni kesik. " + truncation.reason)

    version = instance.server_version_num
    parameterized = has_placeholders(query)
    detail: dict[str, Any] = {
        "server_version_num": version,
        "parameterized": parameterized,
        # EXPLAIN (GENERIC_PLAN) seçeneği PG 16 ile geldi; dbace onu KULLANMIYOR (asyncpg
        # protokolüyle gönderilemiyor, bkz. generic_plan.py) — PG 12+ için PREPARE +
        # force_generic_plan. Bilgi amaçlı taşınıyor ki sürüm farkı arayüzde görülebilsin.
        "generic_plan_option_available": bool(version and version >= PG_VERSION_GENERIC_PLAN),
        "method": "prepare_force_generic_plan" if parameterized else "explain",
    }
    if parameterized and version is not None and version < PG_VERSION_PLAN_CACHE_MODE:
        return _option(
            KIND_GENERIC, False, label=label, detail=detail,
            reason=(
                "Sorgu yer tutucu ($1) içeriyor ve sunucu değerden bağımsız planlamayı "
                "desteklemiyor: gereken plan_cache_mode PostgreSQL 12 ile geldi, bu sunucu daha eski."
            ),
        )
    caveat = plan_source_caveat("manual_estimate")
    if version is None:
        caveat = (caveat or "") + " Sunucu sürümü henüz bilinmiyor (toplama çalışmamış); plan alınırken belli olacak."
    return _option(KIND_GENERIC, True, label=label, caveat=caveat, detail=detail)
