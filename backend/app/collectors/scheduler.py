from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Instance
from app.services.collection import collect_instance, effective_collect_interval, last_collected_at
from app.services.custom_alert_rules import evaluate_custom_alert_rules
from app.services.dashboard_snapshot import refresh_all_group_snapshots
from app.services.retention import run_retention_cleanup
from app.services.rollup import run_daily_rollup
from app.services.health_report import run_scheduled_reports
from app.services.settings import get_dashboard_refresh_interval, get_health_report_schedule
from app.services.prediction_accuracy import evaluate_due_outcomes
from app.services.wait_sampling import sampling_tick, shutdown_sampling

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

REFRESH_JOB_ID = "refresh_dashboard_snapshots"
RETENTION_JOB_ID = "retention_cleanup"
DAILY_ROLLUP_JOB_ID = "daily_rollup"
PREDICTION_ACCURACY_JOB_ID = "prediction_accuracy"
HEALTH_REPORT_JOB_ID = "daily_health_report"
WAIT_SAMPLING_JOB_ID = "wait_event_sampling"
# Fixed tick for custom alert rules — each rule's own interval_seconds is honored inside
# evaluate_custom_alert_rules (per-rule "due" check), not by scheduling one job per rule.
CUSTOM_RULES_TICK_SECONDS = 10


async def collect_all_instances() -> None:
    """Runs on a fixed tick (settings.collect_interval_seconds — this is the minimum
    granularity, not a per-instance guarantee). Each instance is only actually collected once
    its own effective_collect_interval() has elapsed since its last run — an instance with a
    longer override (e.g. a lower-priority/less critical server) is simply skipped on the
    ticks it isn't due yet, same "per-item due-check on a shared tick" pattern
    evaluate_custom_alert_rules already uses for custom alert rules."""
    async with SessionLocal() as session:
        result = await session.execute(select(Instance).where(Instance.enabled.is_(True)))
        instances = result.scalars().all()
        now = datetime.now(UTC)
        for instance in instances:
            last_at = last_collected_at(instance.id)
            if last_at is not None:
                elapsed = (now - last_at).total_seconds()
                if elapsed < effective_collect_interval(instance):
                    continue
            try:
                await collect_instance(instance, session)
            except Exception:
                logger.exception("Failed collecting metrics for instance %s", instance.name)
        await session.commit()


async def refresh_dashboard_snapshots() -> None:
    async with SessionLocal() as session:
        try:
            await refresh_all_group_snapshots(session)
        except Exception:
            logger.exception("Failed refreshing dashboard group health snapshots")


async def evaluate_custom_rules_tick() -> None:
    async with SessionLocal() as session:
        try:
            await evaluate_custom_alert_rules(session)
        except Exception:
            logger.exception("Failed evaluating custom alert rules")


async def retention_cleanup_tick() -> None:
    try:
        await run_retention_cleanup()
    except Exception:
        logger.exception("Failed running retention cleanup")


async def daily_rollup_tick() -> None:
    async with SessionLocal() as session:
        try:
            counts = await run_daily_rollup(session)
            logger.info("Daily rollup: %s metric rows, %s schema object rows", counts["metric_rows"], counts["schema_rows"])
        except Exception:
            logger.exception("Failed running daily rollup")


async def prediction_accuracy_tick() -> None:
    """Faz 20 İŞ 2: hedef tarihi gelmiş tahminleri gerçekleşen değerle karşılaştırır.

    Saatlik çalışıyor çünkü kısa vadeli tahminlerin ufku 1 saat — günlük bir iş onları
    değerlendirilemez hale getirirdi (ham örnekler saklama süresi dolunca siliniyor).
    """
    async with SessionLocal() as session:
        try:
            counts = await evaluate_due_outcomes(session)
            await session.commit()
            if counts["evaluated"] or counts["expired"]:
                logger.info(
                    "Tahmin doğruluğu: %s değerlendirildi, %s ölçülemedi, %s bekliyor",
                    counts["evaluated"], counts["expired"], counts["waiting"],
                )
        except Exception:
            logger.exception("Tahmin doğruluğu değerlendirmesi başarısız")


async def daily_health_report_tick() -> None:
    """Faz 17 İŞ 1: günlük sağlık raporu. Saati ayarlanabilir (varsayılan 06:00).

    Toplama döngüsünü bloke etmemesi için APScheduler'ın kendi cron job'u olarak çalışıyor;
    içeride kapsamlar sırayla üretiliyor ve her biri kendi oturumunu açıyor.
    """
    try:
        produced = await run_scheduled_reports()
        logger.info("Zamanlanmış sağlık raporu: %s rapor üretildi", produced)
    except Exception:
        logger.exception("Zamanlanmış sağlık raporu başarısız")


async def wait_sampling_tick() -> None:
    try:
        await sampling_tick()
    except Exception:
        logger.exception("Bekleme örneklemesi turu başarısız")


async def start_scheduler() -> None:
    if scheduler.running:
        return
    async with SessionLocal() as session:
        refresh_interval = await get_dashboard_refresh_interval(session)
        report_schedule = await get_health_report_schedule(session)

    scheduler.add_job(
        collect_all_instances,
        "interval",
        seconds=settings.collect_interval_seconds,
        id="collect_all",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_dashboard_snapshots,
        "interval",
        seconds=refresh_interval,
        id=REFRESH_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(),
    )
    scheduler.add_job(
        evaluate_custom_rules_tick,
        "interval",
        seconds=CUSTOM_RULES_TICK_SECONDS,
        id="evaluate_custom_rules",
        replace_existing=True,
    )
    # GÜNLÜK İŞLER CRON İLE — "interval" DEĞİL (Faz 21 İŞ 3).
    #
    # `interval(days=1)` ilk çalışmasını scheduler BAŞLADIKTAN 24 saat sonra planlar
    # (APScheduler'ın IntervalTrigger'ı `start_date` verilmezse `now + interval` alır).
    # Worker günde bir kereden sık yeniden başlıyorsa — Railway'de yeniden dağıtım, çökme,
    # platform bakımı — sayaç her seferinde sıfırlanır ve iş HİÇ ÇALIŞMAZ.
    #
    # İki somut sonucu vardı: saklama temizliği çalışmadığı için slow_query_samples sınırsız
    # büyüyordu, ve günlük rollup üretilmediği için uzun vadeli kapasite tahminleri (disk
    # dolma, wraparound, tablo büyümesi) hiç çıkmıyordu — ikisi de sessizce.
    #
    # Cron sabit saate bağlıdır, sürecin ne zaman başladığından bağımsızdır.
    # `misfire_grace_time`: worker o saatte kısa süreli kapalıysa iş yine de yakalanır.
    # `coalesce`: uzun bir kesintiden sonra birikmiş tetiklemeler tek çalışmaya indirilir.
    scheduler.add_job(
        retention_cleanup_tick,
        "cron",
        hour=3,
        minute=0,
        id=RETENTION_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        daily_rollup_tick,
        "cron",
        hour=3,
        minute=30,
        id=DAILY_ROLLUP_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        prediction_accuracy_tick,
        "interval",
        hours=1,
        id=PREDICTION_ACCURACY_JOB_ID,
        replace_existing=True,
        # Saatlik iş de ilk çalışmasını bir saat sonraya planlıyordu; sık yeniden başlatmada
        # aynı sorun küçük ölçekte geçerli.
        next_run_time=datetime.now(),
        coalesce=True,
    )
    if settings.wait_sampling_enabled:
        # BEKLEME ÖRNEKLEMESİ — kendi işi, toplama döngüsünden ayrı (Faz 25 İŞ 1).
        #
        # `max_instances=1`: bir tur (yavaş sunucu, ağ gecikmesi) 1 saniyeyi aşarsa APScheduler
        # varsayılan olarak İKİNCİ bir turu paralel başlatır. Örnekleyici kalıcı bağlantıları ve
        # bellek kovalarını paylaştığı için bu, aynı bağlantı üzerinde iki eşzamanlı sorgu ve
        # bozuk sayaçlar demek. Tek tur garantisi bunu engelliyor.
        #
        # `coalesce=True`: kesintiden sonra birikmiş tetiklemeler tek turda toplanır — geçmişe
        # dönük 300 örnek almanın anlamı yok, örnekleme ANLIK durumu ölçüyor.
        #
        # `next_run_time=now`: ilk tur hemen; IntervalTrigger'ın "now + interval" varsayılanı
        # burada da geçerli (retention/rollup işlerinde canlıda yaşanan sorunun aynısı).
        scheduler.add_job(
            wait_sampling_tick,
            "interval",
            seconds=max(1, settings.wait_sample_interval_seconds),
            id=WAIT_SAMPLING_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(),
        )

    scheduler.add_job(
        daily_health_report_tick,
        "cron",
        hour=report_schedule["hour"],
        minute=0,
        id=HEALTH_REPORT_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Collector scheduler started (collect=%ss, dashboard_refresh=%ss)",
        settings.collect_interval_seconds,
        refresh_interval,
    )


def reschedule_dashboard_refresh(seconds: int) -> None:
    """Applies a new dashboard refresh interval to the live scheduler job (no restart needed).
    A no-op if the scheduler isn't running (run_mode=api) — the persisted setting still takes
    effect the next time a worker process starts."""
    if not scheduler.running:
        return
    if scheduler.get_job(REFRESH_JOB_ID) is None:
        return
    scheduler.reschedule_job(REFRESH_JOB_ID, trigger="interval", seconds=seconds)
    logger.info("Dashboard refresh interval changed to %ss", seconds)


def reschedule_health_report(hour: int) -> None:
    """Rapor saatini canlı scheduler'a uygular (yeniden başlatma gerekmeden).
    Scheduler çalışmıyorsa (run_mode=api) no-op — kaydedilen ayar bir sonraki worker
    başlangıcında zaten devreye girer; reschedule_dashboard_refresh ile aynı desen."""
    if not scheduler.running or scheduler.get_job(HEALTH_REPORT_JOB_ID) is None:
        return
    scheduler.reschedule_job(HEALTH_REPORT_JOB_ID, trigger="cron", hour=hour, minute=0)
    logger.info("Sağlık raporu saati %s:00 olarak değiştirildi", hour)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def stop_scheduler_async() -> None:
    """Kapanış: zamanlayıcıyı durdurur VE örnekleyicinin yarım kalan dakikasını yazar.

    `stop_scheduler()` senkron olduğu için bekleme kovalarını yazamıyor; onları kaybetmek
    kapanış anındaki (yani genelde en ilginç) dakikayı silmek demekti.
    """
    stop_scheduler()
    try:
        await shutdown_sampling()
    except Exception:
        logger.exception("Örnekleyici kapatılırken hata")
