"""Faz 21 İŞ 3 — saklama denetimi ve toplama hacmi.

Denetimde iki gerçek kusur bulundu:

**A. Günlük işler yeniden başlatmada hiç çalışmıyordu.** `interval(days=1)` ilk çalışmasını
scheduler BAŞLADIKTAN 24 saat sonra planlar (APScheduler'ın IntervalTrigger'ı `start_date`
verilmezse `now + interval` alır). Worker günde bir kereden sık yeniden başlıyorsa — Railway'de
yeniden dağıtım, çökme, platform bakımı — sayaç her seferinde sıfırlanır ve iş HİÇ çalışmaz.
İki sessiz sonucu vardı: saklama temizliği yapılmadığı için `slow_query_samples` sınırsız
büyüyordu, ve günlük rollup üretilmediği için uzun vadeli kapasite tahminleri (disk dolma,
wraparound, tablo büyümesi) hiç çıkmıyordu.

**B. Yavaş sorgu toplama oranı hacimle orantısızdı.** Her döngü 20 satır yazıyor; 15 saniyelik
aralıkta instance başına AYDA ~3,5 milyon satır — `metric_samples`'ın 20 katı. pg_stat_statements
kümülatif olduğu için bu çözünürlük analize hiçbir şey katmıyordu.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.collectors import scheduler as sched
from app.config import settings
from app.services import collection
from app.services.retention import ALLOWED_RETENTION_DAYS, DEFAULT_RETENTION_DAYS

# Bir toplama döngüsünde yazılan yavaş sorgu satırı (collectors/postgresql.py, limit=20).
SLOW_QUERY_ROWS_PER_CYCLE = 20


# --- A. Günlük işler yeniden başlatmaya dayanıklı mı ----------------------------------------


def test_interval_trigger_would_never_fire_on_a_restarting_worker():
    """Kusurun KENDİSİ: `interval(days=1)` ilk kez 24 saat sonra çalışır. Bu testin amacı
    davranışı belgelemek — düzeltmenin neden gerekli olduğu buradan okunuyor."""
    trigger = IntervalTrigger(days=1)
    now = datetime.now(UTC).astimezone(trigger.timezone)
    first = trigger.get_next_fire_time(None, now)

    hours = (first - now).total_seconds() / 3600
    assert hours == pytest.approx(24, abs=0.1), (
        "APScheduler davranışı değişmiş olabilir; düzeltmenin gerekçesi bu varsayıma dayanıyor"
    )


@pytest.mark.parametrize(
    "job_getter,label",
    [
        (lambda: sched.RETENTION_JOB_ID, "saklama temizliği"),
        (lambda: sched.DAILY_ROLLUP_JOB_ID, "günlük rollup"),
    ],
)
async def test_daily_jobs_use_cron_so_a_restart_cannot_skip_them(job_getter, label):
    """Cron sabit saate bağlıdır; sürecin ne zaman başladığından bağımsız çalışır."""
    await sched.start_scheduler()
    try:
        job = sched.scheduler.get_job(job_getter())
        assert job is not None, f"{label} işi kayıtlı değil"
        assert isinstance(job.trigger, CronTrigger), (
            f"{label} hâlâ interval kullanıyor — yeniden başlatan bir worker'da hiç çalışmaz"
        )
    finally:
        sched.stop_scheduler()


async def test_daily_jobs_tolerate_a_short_outage():
    """Worker tam o saatte kapalıysa iş büsbütün kaçmasın; uzun kesintide de birikmiş
    tetiklemeler tek çalışmaya insin."""
    await sched.start_scheduler()
    try:
        for job_id in (sched.RETENTION_JOB_ID, sched.DAILY_ROLLUP_JOB_ID):
            job = sched.scheduler.get_job(job_id)
            assert job.misfire_grace_time and job.misfire_grace_time >= 600, job_id
            assert job.coalesce is True, job_id
    finally:
        sched.stop_scheduler()


async def test_the_hourly_accuracy_job_starts_immediately():
    """Saatlik iş de ilk çalışmasını bir saat sonraya planlıyordu; sık yeniden başlatmada
    aynı sorun küçük ölçekte geçerliydi."""
    await sched.start_scheduler()
    try:
        job = sched.scheduler.get_job(sched.PREDICTION_ACCURACY_JOB_ID)
        assert job is not None
        # `next_run_time` verildiği için ilk çalışma bir saat sonrayı BEKLEMEZ.
        assert job.next_run_time is not None
        delay = (job.next_run_time - datetime.now(job.next_run_time.tzinfo)).total_seconds()
        assert delay < 3000, f"ilk çalışma {delay / 60:.0f} dakika sonraya planlanmış"
    finally:
        sched.stop_scheduler()


# --- B. Toplama hacmi -----------------------------------------------------------------------


def _rows_per_month(interval_seconds: int, rows_per_cycle: int = SLOW_QUERY_ROWS_PER_CYCLE) -> float:
    cycles_per_day = 86_400 / interval_seconds
    return cycles_per_day * rows_per_cycle * DEFAULT_RETENTION_DAYS


def test_slow_queries_are_collected_less_often_than_metrics():
    """Ayrı bir aralık olmasının sebebi hacim; metrikler ani olaylar için sık kalmalı."""
    assert settings.slow_query_interval_seconds > settings.collect_interval_seconds


def test_the_default_rate_keeps_the_monthly_row_count_manageable():
    """Eski davranış (her döngüde toplama) instance başına ayda ~3,5 milyon satır üretiyordu."""
    old = _rows_per_month(settings.collect_interval_seconds)
    new = _rows_per_month(settings.slow_query_interval_seconds)

    assert old > 3_000_000, f"eski hacim varsayımı değişmiş: {old:,.0f}"
    assert new < 250_000, f"yeni hacim hâlâ yüksek: {new:,.0f}"
    assert old / new >= 10, "azaltma anlamlı bir mertebede olmalı"


def test_slow_query_collection_is_skipped_until_its_interval_elapses():
    collection._last_slow_query_at.clear()
    now = datetime.now(UTC)
    instance_id = 4242

    assert collection._should_collect_slow_queries(instance_id, now), "ilk döngüde toplanmalı"

    collection._last_slow_query_at[instance_id] = now
    half = now + timedelta(seconds=settings.slow_query_interval_seconds / 2)
    assert not collection._should_collect_slow_queries(instance_id, half), "aralık dolmadan toplandı"

    after = now + timedelta(seconds=settings.slow_query_interval_seconds + 1)
    assert collection._should_collect_slow_queries(instance_id, after), "aralık dolunca toplanmalı"


def test_each_instance_has_its_own_slow_query_clock():
    """Bir instance'ın toplaması diğerininkini bastırmamalı."""
    collection._last_slow_query_at.clear()
    now = datetime.now(UTC)
    collection._last_slow_query_at[1] = now

    assert not collection._should_collect_slow_queries(1, now)
    assert collection._should_collect_slow_queries(2, now)


def test_the_rate_limit_can_be_disabled():
    """0 = her döngüde topla (eski davranış) — ayar geri alınabilir olmalı."""
    original = settings.slow_query_interval_seconds
    try:
        settings.slow_query_interval_seconds = 0
        collection._last_slow_query_at.clear()
        now = datetime.now(UTC)
        collection._last_slow_query_at[7] = now
        assert collection._should_collect_slow_queries(7, now)
    finally:
        settings.slow_query_interval_seconds = original


# --- Saklama politikası hâlâ yerinde mi -----------------------------------------------------


def test_the_retention_default_is_still_one_month():
    """Hacim düzeltmesi saklama süresini kısaltarak yapılmadı — veri kaybı olmamalı."""
    assert DEFAULT_RETENTION_DAYS == 30
    assert DEFAULT_RETENTION_DAYS in ALLOWED_RETENTION_DAYS
