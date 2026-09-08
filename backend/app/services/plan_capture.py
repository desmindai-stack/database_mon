"""Yakalanan planların toplanması ve saklanması (Faz 26 İŞ 1).

`auto_explain.py` log METNİNİ ayrıştırır; bu modül o planları host-agent'tan ÇEKER,
pg_stat_statements kayıtlarıyla eşleştirir ve veritabanına yazar.

NEDEN AYRI BİR İŞ: plan yakalama, metrik toplamaya bağlı değil. Log çekimi host-agent üzerinden
HTTP ile yapılıyor (hedef veritabanına hiç bağlanılmıyor) ve auto_explain eşiği genelde
saniyeler mertebesinde — 15 saniyede bir log çekmenin anlamı yok, üstelik her çekim aynı
satırları tekrar okuyor. Varsayılan 5 dakika.

TEKRAR YAZMA KORUMASI: her çekim log'un SON N satırını okuyor, yani pencereler örtüşüyor ve
aynı plan birden çok kez görülüyor. Model üzerindeki
(instance, captured_at, duration_ms, fingerprint) UNIQUE kısıtı bunu engelliyor; kısıt yerine
"son ne zaman çektik" saymacı kullanmak, worker yeniden başladığında çöker ve aynı planlar
ikinci kez yazılırdı.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.domain.engines import DatabaseEngine
from app.models import CapturedPlan, DeadlockEvent, Instance, SlowQuerySample
from app.services.auto_explain import (
    CapturedPlanRecord,
    normalize_query_key,
    parse_auto_explain_log,
)
from app.services.cluster_health import fetch_agent_logs
from app.collectors.registry import get_collector
from app.services.collection import connection_target_for
from app.services.deadlocks import (
    DeadlockRecord,
    parse_postgres_deadlocks,
    parse_sqlserver_deadlock_rows,
)

logger = logging.getLogger(__name__)

#: Her çekimde okunacak log satırı sayısı. Host-agent'ın üst sınırı 500 (routers/instances.py
#: cluster-logs ucu da aynı sınırı kullanıyor); bir plan onlarca satır tuttuğu için bu, tur
#: başına birkaç düzine plana denk geliyor.
LOG_LINES_PER_FETCH = 500

#: Sorgu metni bu uzunluktan sonra kırpılıyor. Tam metin planın içinde ("Query Text") zaten
#: duruyor; bu alan listeleme ve eşleştirme için.
MAX_QUERY_TEXT = 4000

#: Eşleştirme için pg_stat_statements'ta geriye kaç saat bakılacağı. Daha eskisine bakmak,
#: artık çalışmayan bir sorguya plan bağlama riski demek.
MATCH_LOOKBACK_HOURS = 24


def fingerprint(query: str) -> str:
    """Normalleştirilmiş sorgunun kısa özeti (hex). Tekrar tespitinde ve eşleştirmede
    kullanılıyor; kriptografik bir amaç yok, çakışma olasılığı bu ölçekte ihmal edilebilir."""
    return hashlib.sha256(normalize_query_key(query).encode("utf-8")).hexdigest()[:32]


async def _build_queryid_index(session: AsyncSession, instance_id: int) -> dict[str, str]:
    """fingerprint -> queryid haritası, son toplanan yavaş sorgulardan.

    auto_explain log'u queryid YAZMIYOR (PostgreSQL 16'da `log_line_prefix`'e `%Q` eklenebilir
    ama her kurulumda yok, 16 öncesinde hiç yok). Bu yüzden eşleştirme metin üzerinden
    yapılıyor ve KESİN DEĞİL. Eşleşme bulunamazsa plan yine kaydediliyor — yanlış bir sorguya
    bağlamaktansa bağlamamak yeğdir.
    """
    since = datetime.now(UTC) - timedelta(hours=MATCH_LOOKBACK_HOURS)
    rows = (
        await session.execute(
            select(SlowQuerySample.queryid, SlowQuerySample.query)
            .where(
                SlowQuerySample.instance_id == instance_id,
                SlowQuerySample.collected_at >= since,
                SlowQuerySample.queryid.is_not(None),
            )
            .limit(2000)
        )
    ).all()
    index: dict[str, str] = {}
    for queryid, query in rows:
        if not queryid or not query:
            continue
        index.setdefault(fingerprint(query), str(queryid))
    return index


async def store_captured_plans(
    session: AsyncSession,
    instance_id: int,
    records: list[CapturedPlanRecord],
    *,
    source: str = "auto_explain",
) -> int:
    """Planları yazar, zaten var olanları atlar. Yazılan satır sayısını döner."""
    if not records:
        return 0
    queryid_index = await _build_queryid_index(session, instance_id)

    written = 0
    for record in records:
        key = fingerprint(record.query_text)
        exists = (
            await session.execute(
                select(CapturedPlan.id).where(
                    CapturedPlan.instance_id == instance_id,
                    CapturedPlan.captured_at == record.captured_at,
                    CapturedPlan.duration_ms == record.duration_ms,
                    CapturedPlan.query_fingerprint == key,
                )
            )
        ).first()
        if exists:
            continue
        session.add(
            CapturedPlan(
                instance_id=instance_id,
                captured_at=record.captured_at,
                source=source,
                duration_ms=record.duration_ms,
                query_text=record.query_text[:MAX_QUERY_TEXT],
                query_fingerprint=key,
                queryid=queryid_index.get(key),
                has_actual_rows=record.has_actual_rows,
                plan_json=record.plan_json,
            )
        )
        written += 1
    return written


def agent_configured(instance: Instance) -> bool:
    return bool((instance.options or {}).get("agent_url"))


async def capture_plans_for_instance(session: AsyncSession, instance: Instance) -> dict:
    """Tek instance için log çekip planları yazar.

    Dönen sözlük teşhis içindir: kaç plan bulundu, kaçı yeni, ayrıştırılamayan var mı. "Neden
    hiç plan gelmiyor" sorusu log'a bakmadan cevaplanabilsin diye.
    """
    outcome = {"found": 0, "written": 0, "unparsed": 0, "deadlocks": 0, "error": None, "note": None}
    if instance.engine != str(DatabaseEngine.POSTGRESQL):
        outcome["error"] = "auto_explain yalnızca PostgreSQL için geçerli"
        return outcome
    if not agent_configured(instance):
        # Log erişimi host-agent üzerinden. Yoksa bu bir HATA değil, yapılandırma eksikliği —
        # yönetilen servislerde zaten hiç mümkün olmayacak.
        outcome["error"] = "host-agent yapılandırılmamış — sunucu log'una erişilemiyor"
        return outcome

    try:
        payload = await fetch_agent_logs(instance.options or {}, service="postgresql", lines=LOG_LINES_PER_FETCH)
    except Exception as exc:
        outcome["error"] = f"log çekilemedi: {exc}"
        return outcome

    lines = payload.get("lines") or []
    if not isinstance(lines, list):
        outcome["error"] = "agent beklenmedik bir log yanıtı döndü"
        return outcome

    text_lines = [str(line) for line in lines]
    parsed = parse_auto_explain_log(text_lines)
    outcome["found"] = len(parsed.plans)
    outcome["unparsed"] = parsed.unparsed_blocks
    outcome["note"] = parsed.note
    outcome["written"] = await store_captured_plans(session, instance.id, parsed.plans)

    # AYNI LOG ÇEKİMİNDEN deadlock'lar da ayrıştırılıyor (Faz 26 İŞ 3). İkinci bir çekim
    # yapmak, aynı satırları ağdan iki kez geçirmek ve agent'a iki kat istek atmak olurdu.
    # Deadlock yalnızca log'dan görülebilir: veritabanı döngüyü kırar ve olay anlıktır,
    # canlı ekranda hiçbir izi kalmaz.
    outcome["deadlocks"] = await store_deadlocks(
        session, instance.id, parse_postgres_deadlocks(text_lines)
    )
    return outcome


async def store_deadlocks(
    session: AsyncSession, instance_id: int, records: list[DeadlockRecord]
) -> int:
    """Deadlock olaylarını yazar, zaten var olanları atlar."""
    written = 0
    for record in records:
        exists = (
            await session.execute(
                select(DeadlockEvent.id).where(
                    DeadlockEvent.instance_id == instance_id,
                    DeadlockEvent.detected_at == record.detected_at,
                    DeadlockEvent.fingerprint == record.fingerprint,
                )
            )
        ).first()
        if exists:
            continue
        session.add(
            DeadlockEvent(
                instance_id=instance_id,
                detected_at=record.detected_at,
                source=record.source,
                fingerprint=record.fingerprint,
                victim_pid=record.victim_pid,
                victim_query=record.victim_query[:4000],
                winner_pid=record.winner_pid,
                winner_query=record.winner_query[:4000],
                participants=record.participants,
                raw_detail=record.raw_detail,
            )
        )
        written += 1
    return written


async def capture_sqlserver_deadlocks(session: AsyncSession, instance: Instance) -> dict:
    """SQL Server deadlock'larını system_health oturumundan toplar.

    PostgreSQL'den FARKLI bir yol: orada deadlock sunucu log'una yazılıyor ve host-agent
    üzerinden okunuyor. SQL Server'da log yok, olaylar `system_health` genişletilmiş olay
    oturumunun halka tamponunda duruyor ve oraya SORGUYLA erişiliyor — yani host-agent
    gerekmiyor, hedef veritabanına bağlanılıyor.

    Halka tamponu DÖNGÜSEL: eski olaylar zamanla düşüyor. Periyodik okuma bu yüzden şart;
    "gerektiğinde bakarız" demek, olayın kaybolmasından sonra bakmak demek.
    """
    outcome = {"found": 0, "written": 0, "error": None}
    if instance.engine != str(DatabaseEngine.SQLSERVER):
        outcome["error"] = "system_health yalnızca SQL Server için geçerli"
        return outcome

    collector = get_collector(DatabaseEngine(instance.engine), connection_target_for(instance))
    try:
        rows = await collector.collect_deadlocks()
    except Exception as exc:
        # XE oturumu kapalı olabilir ya da yetki yetmeyebilir. Toplamanın geri kalanını
        # düşürmemeli: deadlock geçmişi bir ek yetenek.
        outcome["error"] = f"deadlock okunamadı: {exc}"
        return outcome

    records = parse_sqlserver_deadlock_rows(rows)
    outcome["found"] = len(records)
    outcome["written"] = await store_deadlocks(session, instance.id, records)
    return outcome


async def capture_plans_tick() -> dict:
    """Zamanlayıcı turu: auto_explain açık olabilecek tüm PostgreSQL instance'ları."""
    if not settings.plan_capture_enabled:
        return {"instances": 0, "written": 0}
    totals = {"instances": 0, "written": 0, "deadlocks": 0}
    async with SessionLocal() as session:
        instances = (
            await session.execute(
                select(Instance).where(
                    Instance.enabled.is_(True),
                    Instance.engine.in_(
                        [str(DatabaseEngine.POSTGRESQL), str(DatabaseEngine.SQLSERVER)]
                    ),
                )
            )
        ).scalars().all()
        for instance in instances:
            # İKİ MOTOR, İKİ AYRI YOL. PostgreSQL'de planlar ve deadlock'lar sunucu log'undan
            # (host-agent üzerinden) okunuyor; SQL Server'da deadlock'lar system_health
            # oturumundan SORGUYLA alınıyor ve agent gerekmiyor. Aynı işte toplanmalarının
            # sebebi ikisinin de "geriye dönük olay yakalama" olması ve aynı seyrek aralığın
            # (5 dakika) ikisine de uygun düşmesi.
            if instance.engine == str(DatabaseEngine.SQLSERVER):
                totals["instances"] += 1
                try:
                    outcome = await capture_sqlserver_deadlocks(session, instance)
                    totals["deadlocks"] += outcome["written"]
                    if outcome["error"]:
                        logger.debug(
                            "SQL Server deadlock toplama atlandı (%s): %s",
                            instance.name, outcome["error"],
                        )
                except Exception:
                    logger.exception("Deadlock toplama başarısız (instance %s)", instance.name)
                continue

            if not agent_configured(instance):
                continue
            totals["instances"] += 1
            try:
                outcome = await capture_plans_for_instance(session, instance)
                totals["written"] += outcome["written"]
                totals["deadlocks"] += outcome.get("deadlocks", 0)
                if outcome["error"]:
                    logger.debug("Plan yakalama atlandı (%s): %s", instance.name, outcome["error"])
            except Exception:
                logger.exception("Plan yakalama başarısız (instance %s)", instance.name)
        await session.commit()
    return totals
