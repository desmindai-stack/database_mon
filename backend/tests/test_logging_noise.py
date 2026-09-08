"""Log gürültüsü (Faz 27 İŞ 2).

SORUN: bekleme örnekleyicisi saniyede bir çalışıyor ve APScheduler her tetiklemeyi INFO
seviyesinde logluyordu — Railway'de günde 86.400 satır. Gerçek hatalar bu yığının içinde
kayboluyor; log'un varlık sebebi ise tam olarak onları görebilmek.

Bu dosya iki şeyi kilitliyor:

1. **Sık çalışan işler tur başına log yazmıyor.** Yazılanlar: 5 dakikalık ÖZET ve tek tek
   anlamlı olaylar (gecikmiş tur, bağlantı kopması).
2. **Susturma hataları gizlemiyor.** APScheduler'ı WARNING'e çekmek iş hatalarını
   kaybetmek anlamına gelmiyor — o zaten hataları ERROR olarak yazıyor.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.logging_setup import DEFAULT_LEVEL, NOISY_LOGGERS, configure_logging, resolve_level
from app.services import wait_sampling


@pytest.fixture(autouse=True)
def _reset():
    wait_sampling.reset_state()
    yield
    wait_sampling.reset_state()
    configure_logging(DEFAULT_LEVEL)


# --- Seviye çözümü ------------------------------------------------------------------------


def test_valid_levels_are_honored():
    assert resolve_level("DEBUG") == logging.DEBUG
    assert resolve_level("warning") == logging.WARNING
    assert resolve_level("  Error  ") == logging.ERROR


def test_an_unknown_level_falls_back_instead_of_silencing_everything():
    """`LOG_LEVEL=verbose` yüzünden log'un tamamen susması, teşhis edilmesi en zor
    durumlardan biri olurdu."""
    assert resolve_level("verbose") == logging.INFO


def test_empty_level_falls_back():
    assert resolve_level("") == logging.INFO


# --- Gürültülü logger'lar -----------------------------------------------------------------


def test_apscheduler_per_run_logging_is_silenced_at_info():
    """APScheduler her iş çalıştırmasında iki satır yazıyor. Saniyede bir çalışan bir işte
    bu tek başına günde ~172 bin satır."""
    configure_logging("INFO")
    assert logging.getLogger("apscheduler.executors.default").level == logging.WARNING


def test_silencing_does_not_hide_job_failures():
    """Susturma WARNING'e çekiyor, kapatmıyor: APScheduler bir iş exception fırlattığında
    zaten ERROR yazıyor ve ERROR > WARNING olduğu için görünür kalıyor."""
    configure_logging("INFO")
    noisy = logging.getLogger("apscheduler.executors.default")
    assert noisy.isEnabledFor(logging.ERROR)
    assert noisy.isEnabledFor(logging.WARNING)
    assert not noisy.isEnabledFor(logging.INFO)


def test_debug_level_lifts_the_silencing():
    """DEBUG'a çeken kişi teşhis yapıyordur ve gürültüyü de istiyordur; orada susturmak
    onu aradığı satırdan mahrum bırakırdı."""
    configure_logging("DEBUG")
    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG, name


def test_every_noisy_logger_is_silenced_to_warning_or_stricter():
    configure_logging("INFO")
    for name, level in NOISY_LOGGERS.items():
        assert level >= logging.WARNING, name


# --- Örnekleyici özet loglaması -----------------------------------------------------------


def _round(now: datetime, duration: float = 0.01, instances: int = 2) -> None:
    wait_sampling._record_round(now, duration, instances)


def test_a_normal_round_writes_nothing(caplog):
    """TUR BAŞINA LOG YOK. Bu testin düşmesi, günde 86.400 satırın geri geldiği anlamına
    gelir."""
    base = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
    with caplog.at_level(logging.DEBUG, logger="app.services.wait_sampling"):
        for i in range(120):
            _round(base + timedelta(seconds=i))
    assert caplog.records == []


def test_a_summary_is_written_every_five_minutes(caplog):
    base = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        _round(base)
        for i in range(1, 400):
            _round(base + timedelta(seconds=i))
    messages = [r.message for r in caplog.records]
    assert len(messages) == 1, f"beklenen tek özet, gelen: {messages}"
    assert "özeti" in messages[0]


def test_the_summary_carries_counts_and_latency(caplog):
    base = datetime(2026, 9, 13, 11, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        _round(base, duration=0.02)
        for i in range(1, 320):
            _round(base + timedelta(seconds=i), duration=0.02)
    record = caplog.records[0]
    rendered = record.getMessage()
    assert "tur" in rendered and "ortalama" in rendered and "en yavaş" in rendered


def test_a_slow_round_is_reported_on_its_own(caplog):
    """Gecikmiş tur ANLAMLI bir olay: ölçümde delik açıyor ve AAS'in paydasını düşürüyor.
    Özeti beklemek, sorunun 5 dakika görünmez kalması demekti."""
    base = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="app.services.wait_sampling"):
        _round(base, duration=10.0)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "gecikti" in caplog.records[0].getMessage()


def test_a_fast_round_is_not_reported_as_slow(caplog):
    base = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="app.services.wait_sampling"):
        _round(base, duration=0.5)
    assert caplog.records == []


def test_slow_rounds_are_counted_in_the_summary(caplog):
    base = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        _round(base)
        _round(base + timedelta(seconds=1), duration=9.0)
        _round(base + timedelta(seconds=400))
    summaries = [r.getMessage() for r in caplog.records if "özeti" in r.getMessage()]
    assert summaries
    assert "1 gecikmiş tur" in summaries[0]


def test_counters_reset_after_each_summary(caplog):
    base = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="app.services.wait_sampling"):
        _round(base)
        _round(base + timedelta(seconds=400))
        _round(base + timedelta(seconds=800))
    summaries = [r.getMessage() for r in caplog.records if "özeti" in r.getMessage()]
    assert len(summaries) == 2
    # İkinci özet, birincinin turlarını tekrar saymamalı.
    assert "1 tur" in summaries[1]


# --- Sık çalışan diğer işler ---------------------------------------------------------------


def test_frequent_scheduler_jobs_do_not_log_per_tick():
    """10 saniyede bir çalışan alarm değerlendirmesi ve 15 saniyede bir çalışan toplama
    döngüsü, yalnızca HATA durumunda log yazmalı. Aksi halde örnekleyicide çözdüğümüz
    gürültü sorunu başka bir kapıdan geri gelir."""
    import inspect

    from app.collectors import scheduler as scheduler_module

    for name in ("evaluate_custom_rules_tick", "collect_all_instances", "wait_sampling_tick"):
        source = inspect.getsource(getattr(scheduler_module, name))
        assert "logger.info(" not in source, f"{name} tur başına INFO yazıyor"
