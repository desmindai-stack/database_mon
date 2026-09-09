"""Yedek durumu değerlendirmesi ve raporlanması (Faz 28 İŞ 1b).

İŞ 1a "ne toplandı"yı korudu; bu dosya "toplanandan ne sonuç çıkarıldı"yı koruyor.

Korunan asıl fikir, bu turda da aynı: **üç durum vardır, iki değil.** Yedek var ve güncel;
yedek yok; ve dbace göremedi. Üçüncüsünü ikincisi gibi göstermek gerçekten yedeği olan kurumu
paniğe sürükler, ikincisini birincisi gibi göstermek ise felaket anında geri dönüşü olmayan
bir sürprize. `sla_ok` bu yüzden üç değerli ve `None` asla `False` gibi işlenmiyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database import SessionLocal, init_db
from app.domain.backups import (
    DURATION_ANOMALY_MIN_SECONDS,
    FAILURE_STICKY_HOURS,
    SLOT_RETAINED_WAL_CRITICAL_BYTES,
    SLOT_RETAINED_WAL_WARNING_BYTES,
    BackupSource,
    BackupStatus,
    BackupType,
    failure_sticky_hours,
)
from app.models import BackupProbe, BackupRecord, Instance
from app.services import health_report as hr
from app.services.backup_health import assess_instance
from app.services.credentials import encrypt_secret
from app.services.executive_report import _backup_from_sections, assert_no_technical_leak
from app.services.report_documents import _fmt_backup_age
from app.services.report_sections import backup_section

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _instance(engine: str = "sqlserver", options: dict | None = None):
    """Değerlendirme saf bir fonksiyon; ORM nesnesine ihtiyacı yok."""
    return SimpleNamespace(id=1, name="db1", engine=engine, options=options or {})


def _record(
    *,
    backup_type=BackupType.FULL,
    status=BackupStatus.SUCCESS,
    started_hours_ago: float = 1.0,
    duration: float | None = 60.0,
    size: float | None = None,
    database_name: str | None = None,
    source=BackupSource.MSDB,
    error: str | None = None,
    detail: dict | None = None,
):
    started = NOW - timedelta(hours=started_hours_ago)
    return SimpleNamespace(
        backup_type=str(backup_type),
        status=str(status),
        started_at=started,
        finished_at=None if status == BackupStatus.RUNNING else started + timedelta(seconds=duration or 0),
        duration_seconds=duration,
        size_bytes=size,
        database_name=database_name,
        source=str(source),
        error_message=error,
        detail=detail or {},
    )


def _probe(
    *,
    errors: dict | None = None,
    archiver: dict | None = None,
    slots: list | None = None,
    recovery_models: list | None = None,
    checked: list | None = None,
    found: list | None = None,
):
    return SimpleNamespace(
        probed_at=NOW,
        methods_checked=checked or [],
        methods_found=found or [],
        archiver=archiver,
        slots=slots or [],
        recovery_models=recovery_models or [],
        errors=errors or {},
    )


def _keys(assessment) -> set[str]:
    return {i.key for i in assessment.issues}


def _issue(assessment, prefix: str):
    return next(i for i in assessment.issues if i.key.startswith(prefix))


# --- "Yedek yok" ile "dbace bulamadı" ayrımı ---------------------------------------------


def test_missing_backup_is_critical_only_when_the_source_was_actually_readable():
    """msdb okunabildiyse ve boşsa gerçekten yedek yok demektir — bu kritik."""
    assessment = assess_instance(
        _instance("sqlserver"), [], _probe(checked=["msdb"]), now=NOW
    )
    assert assessment.conclusive is True
    issue = _issue(assessment, "backup_not_found")
    assert issue.severity == "critical"
    assert "Yedek bulunamadı" in issue.title


def test_unreadable_source_never_claims_there_is_no_backup():
    """En pahalı yanlışlardan biri: yedeği olan kuruma "yedeğiniz yok" demek."""
    assessment = assess_instance(
        _instance("sqlserver"),
        [],
        _probe(errors={"msdb": "SELECT permission denied"}, checked=["msdb"]),
        now=NOW,
    )
    assert assessment.conclusive is False
    issue = _issue(assessment, "backup_not_found")
    assert issue.severity == "warning"
    assert "belirlenemedi" in issue.title.lower()
    # "Yedek yok" iddiası HİÇBİR yerde geçmemeli.
    assert "ANLAMINA GELMEZ" in issue.detail


def test_postgresql_without_an_agent_is_never_conclusive():
    """PostgreSQL'de gerçek yedekler harici araçlarla alınıyor ve agent olmadan görülemiyor.
    Agent yokken "yedek yok" demek, PostgreSQL tarafında yapılabilecek en kolay yanlış."""
    assessment = assess_instance(
        _instance("postgresql"),
        [],
        _probe(errors={"backup_tools": "Host-agent yapılandırılmamış"}, checked=["pg_stat_archiver"]),
        now=NOW,
    )
    assert assessment.conclusive is False
    assert assessment.status == "unknown"
    assert assessment.sla_ok is None


def test_sla_unknown_is_not_the_same_as_sla_breached():
    """`None` ile `False` karıştırılırsa yönetici raporu sahte alarm üretir."""
    unknown = assess_instance(
        _instance("postgresql"), [], _probe(errors={"backup_tools": "yok"}), now=NOW
    )
    breached = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=24 * 30)],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert unknown.sla_ok is None
    assert breached.sla_ok is False


def test_not_found_advice_lists_the_methods_that_were_tried():
    """"Yedek bulunamadı" demek yetmez: kullanıcı NEREYE bakıldığını bilmeli."""
    assessment = assess_instance(
        _instance("postgresql"),
        [],
        _probe(checked=["pg_stat_archiver", "pgbackrest"], errors={"backup_tools": "yok"}),
        now=NOW,
    )
    advice = _issue(assessment, "backup_not_found").advice
    joined = " ".join(advice.cautions)
    assert "pgBackRest" in joined and "WAL arşivleyici" in joined


# --- Yedek yaşı ---------------------------------------------------------------------------


def test_full_backup_age_crosses_warning_then_critical():
    warn = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=24 * 9)],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    crit = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=24 * 20)],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert _issue(warn, "backup_age:full").severity == "warning"
    assert _issue(crit, "backup_age:full").severity == "critical"


def test_fresh_backup_produces_no_age_finding():
    assessment = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=2)],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_age") for k in _keys(assessment))
    assert assessment.sla_ok is True


def test_per_instance_thresholds_are_honoured():
    """Ödeme sistemiyle raporlama veritabanı aynı eşiği paylaşamaz."""
    records = [_record(started_hours_ago=30)]
    default = assess_instance(_instance("sqlserver"), records, _probe(checked=["msdb"]), now=NOW)
    strict = assess_instance(
        _instance("sqlserver", {"backup_thresholds": {"full": {"warning_hours": 24, "critical_hours": 48}}}),
        records,
        _probe(checked=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_age") for k in _keys(default))
    assert _issue(strict, "backup_age:full").severity == "warning"


def test_backup_age_uses_finish_time_not_start_time():
    """6 saat süren bir yedeği başlangıcına göre ölçmek, onu 6 saat daha taze gösterir."""
    # Başlangıç eşiğin (8 gün) 6 saat ötesinde, bitiş ise 1 saat berisinde.
    assessment = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=24 * 8 + 6, duration=7 * 3600)],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    # Başlangıç eşiği aşıyor ama bitiş aşmıyor: bulgu ÜRETİLMEMELİ.
    assert not any(k.startswith("backup_age:full") for k in _keys(assessment))


def test_log_threshold_is_not_applied_when_there_is_no_log_chain_at_all():
    """SIMPLE recovery ya da arşivlemesiz bir kurulumda saat başı kritik üretmek gürültüdür;
    "hiç log yedeği yok" durumu recovery model ve arşiv bulgularının işi."""
    assessment = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=1)],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_age:log") for k in _keys(assessment))


def test_stale_log_backup_is_reported():
    assessment = assess_instance(
        _instance("sqlserver"),
        [
            _record(started_hours_ago=1),
            _record(backup_type=BackupType.LOG, started_hours_ago=10),
        ],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert _issue(assessment, "backup_age:log").severity == "critical"


# --- Başarısız yedek: yapışkan ama sonsuz değil -------------------------------------------


def test_recent_failure_is_reported():
    assessment = assess_instance(
        _instance("sqlserver"),
        [
            _record(status=BackupStatus.FAILED, started_hours_ago=1, error="disk dolu"),
            _record(started_hours_ago=26),
        ],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    issue = _issue(assessment, "backup_failed")
    assert issue.severity == "critical"
    assert "disk dolu" in " ".join(f["value"] for f in issue.facts)


def test_failure_is_cleared_by_a_later_success_not_only_by_time():
    """Yalnızca zamana bakmak, 15 dakika sonra düzelmiş bir sorunu 24 saat göstermek demekti."""
    assessment = assess_instance(
        _instance("sqlserver"),
        [
            _record(status=BackupStatus.FAILED, started_hours_ago=5, error="disk dolu"),
            _record(started_hours_ago=1),
        ],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_failed") for k in _keys(assessment))


def test_old_failure_stops_being_current():
    """Altı ay önceki bir hata sonsuza kadar kırmızı yanarsa alarm körlüğü yaratır."""
    assessment = assess_instance(
        _instance("sqlserver"),
        [
            _record(started_hours_ago=1),
            _record(
                status=BackupStatus.FAILED,
                started_hours_ago=FAILURE_STICKY_HOURS + 10,
                error="eski hata",
            ),
        ],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_failed") for k in _keys(assessment))


def test_failure_sticky_window_is_configurable_per_instance():
    assert failure_sticky_hours({"backup_failure_sticky_hours": 2}) == 2
    # Sıfır "başarısızlığı hiç gösterme" demek olurdu — sessizce kabul edilmiyor.
    assert failure_sticky_hours({"backup_failure_sticky_hours": 0}) == FAILURE_STICKY_HOURS
    assert failure_sticky_hours({"backup_failure_sticky_hours": "abc"}) == FAILURE_STICKY_HOURS


# --- Süre anomalisi: oran VE statik taban BİRLİKTE ----------------------------------------


def _duration_records(latest_seconds: float, baseline_seconds: float, count: int = 5):
    rows = [_record(started_hours_ago=1, duration=latest_seconds)]
    rows += [
        _record(started_hours_ago=24 * (i + 1), duration=baseline_seconds) for i in range(count)
    ]
    return rows


def test_small_durations_never_raise_a_duration_alarm():
    """4 saniyelik bir yedeğin 9 saniyeye çıkması "2 kat yavaşladı" ama kimse bunun için
    uyandırılmamalı. Statik taban olmadan bu alarm sürekli çalardı."""
    assessment = assess_instance(
        _instance("sqlserver"),
        _duration_records(latest_seconds=9, baseline_seconds=4),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_duration") for k in _keys(assessment))


def test_long_duration_alone_is_not_enough_either():
    """Statik tabanı aşan ama ortalamayla uyumlu bir süre normaldir — büyük veritabanlarında
    yedek zaten uzun sürer ve bunu sürekli alarma çevirmek gürültüdür."""
    long = DURATION_ANOMALY_MIN_SECONDS * 3
    assessment = assess_instance(
        _instance("sqlserver"),
        _duration_records(latest_seconds=long, baseline_seconds=long),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_duration") for k in _keys(assessment))


def test_duration_alarm_fires_when_both_conditions_hold():
    assessment = assess_instance(
        _instance("sqlserver"),
        _duration_records(
            latest_seconds=DURATION_ANOMALY_MIN_SECONDS * 4,
            baseline_seconds=DURATION_ANOMALY_MIN_SECONDS,
        ),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert _issue(assessment, "backup_duration").severity == "warning"


def test_duration_needs_a_baseline_before_it_judges():
    """Kanıt yetersizken bulgu üretmek yasak (CLAUDE.md kanıt kuralı)."""
    assessment = assess_instance(
        _instance("sqlserver"),
        _duration_records(
            latest_seconds=DURATION_ANOMALY_MIN_SECONDS * 4,
            baseline_seconds=DURATION_ANOMALY_MIN_SECONDS,
            count=1,
        ),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_duration") for k in _keys(assessment))


def test_running_backup_is_measured_too():
    """"Devam eden yedeğin süresi" isteğin açık bir parçasıydı."""
    rows = [
        _record(
            status=BackupStatus.RUNNING,
            started_hours_ago=2,
            duration=DURATION_ANOMALY_MIN_SECONDS * 5,
        )
    ]
    rows += [
        _record(started_hours_ago=24 * (i + 1), duration=DURATION_ANOMALY_MIN_SECONDS)
        for i in range(4)
    ]
    assessment = assess_instance(
        _instance("sqlserver"), rows, _probe(checked=["msdb"], found=["msdb"]), now=NOW
    )
    issue = _issue(assessment, "backup_duration")
    assert "sürüyor" in issue.title
    assert assessment.running and assessment.running[0]["backup_type"] == str(BackupType.FULL)


# --- Boyut anomalisi ----------------------------------------------------------------------


def _sized_records(latest: float, baseline: float):
    rows = [_record(started_hours_ago=1, size=latest)]
    rows += [_record(started_hours_ago=24 * (i + 1), size=baseline) for i in range(4)]
    return rows


def test_shrinking_backup_is_more_serious_than_a_growing_one():
    """Yarıya inen bir yedek genelde EKSİK yedek demek ve bu, geri dönüş anında — yani
    yapılabilecek hiçbir şeyin kalmadığı anda — fark edilir."""
    shrink = assess_instance(
        _instance("sqlserver"),
        _sized_records(latest=10.0, baseline=100.0),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    grow = assess_instance(
        _instance("sqlserver"),
        _sized_records(latest=300.0, baseline=100.0),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert _issue(shrink, "backup_size").severity == "critical"
    assert _issue(grow, "backup_size").severity == "warning"


def test_normal_size_variation_is_not_an_anomaly():
    assessment = assess_instance(
        _instance("sqlserver"),
        _sized_records(latest=110.0, baseline=100.0),
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert not any(k.startswith("backup_size") for k in _keys(assessment))


# --- WAL arşivleme ------------------------------------------------------------------------


def test_archiving_disabled_is_reported():
    assessment = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(checked=["pg_stat_archiver"], archiver={"archive_mode": "off"}),
        now=NOW,
    )
    assert "wal_archiving_off" in _keys(assessment)


def test_archiver_failure_counter_alone_does_not_raise_an_alarm():
    """`failed_count` istatistik sıfırlanana kadar birikir; geçmişte bir kez hata almış olmak
    arşivlemenin BOZUK olduğu anlamına gelmez. Belirleyici olan zaman damgası."""
    assessment = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(
            checked=["pg_stat_archiver"],
            archiver={
                "archive_mode": "on",
                "failed_count": 12,
                "last_failed_time": (NOW - timedelta(days=3)).isoformat(),
                "last_archived_time": (NOW - timedelta(minutes=2)).isoformat(),
            },
        ),
        now=NOW,
    )
    assert "wal_archiving_failing" not in _keys(assessment)


def test_archiving_currently_failing_is_critical():
    assessment = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(
            checked=["pg_stat_archiver"],
            archiver={
                "archive_mode": "on",
                "failed_count": 3,
                "last_failed_time": (NOW - timedelta(minutes=5)).isoformat(),
                "last_archived_time": (NOW - timedelta(hours=6)).isoformat(),
            },
        ),
        now=NOW,
    )
    assert _issue(assessment, "wal_archiving_failing").severity == "critical"


def test_wal_archive_time_counts_as_the_log_chain_for_postgresql():
    """PostgreSQL'de log yedeğinin karşılığı sürekli arşivleme; yaşı bir kayıt satırında
    değil pg_stat_archiver'da duruyor."""
    assessment = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(
            checked=["pg_stat_archiver"],
            archiver={
                "archive_mode": "on",
                "last_archived_time": (NOW - timedelta(hours=9)).isoformat(),
            },
        ),
        now=NOW,
    )
    assert assessment.last_log_at is not None
    assert _issue(assessment, "backup_age:log").severity == "critical"


# --- Replikasyon slotları -----------------------------------------------------------------


def test_slot_accumulation_escalates_with_size():
    warn = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(slots=[{"slot_name": "s1", "retained_bytes": SLOT_RETAINED_WAL_WARNING_BYTES + 1, "active": True}]),
        now=NOW,
    )
    crit = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(slots=[{"slot_name": "s1", "retained_bytes": SLOT_RETAINED_WAL_CRITICAL_BYTES + 1, "active": False}]),
        now=NOW,
    )
    assert _issue(warn, "replication_slot").severity == "warning"
    assert _issue(crit, "replication_slot").severity == "critical"


def test_lost_slot_is_critical_regardless_of_size():
    """`lost` = gereken WAL zaten silinmiş; slota güvenen replika artık devam edemez."""
    assessment = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(slots=[{"slot_name": "s1", "wal_status": "lost", "retained_bytes": 0, "active": False}]),
        now=NOW,
    )
    assert _issue(assessment, "replication_slot").severity == "critical"


def test_slot_advice_warns_before_dropping():
    """Kullanımdaki bir slotu silmek bağlı replikayı yeniden kurmayı gerektirir."""
    assessment = assess_instance(
        _instance("postgresql"),
        [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
        _probe(slots=[{"slot_name": "s1", "retained_bytes": SLOT_RETAINED_WAL_CRITICAL_BYTES + 1, "active": True}]),
        now=NOW,
    )
    cautions = " ".join(_issue(assessment, "replication_slot").advice.cautions)
    assert "yeniden kurmayı" in cautions


# --- Recovery model uyumu -----------------------------------------------------------------


def test_full_recovery_without_log_backups_is_reported():
    assessment = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=1)],
        _probe(
            checked=["msdb"],
            found=["msdb"],
            recovery_models=[{"database_name": "odeme", "recovery_model": "FULL", "last_log_at": None}],
        ),
        now=NOW,
    )
    issue = _issue(assessment, "recovery_model_mismatch")
    assert "odeme" in issue.title
    # Her iki yol da sunulmalı: log yedeği almak VEYA SIMPLE'a geçmek.
    actions = " ".join(s.action for s in issue.advice.steps)
    assert "SIMPLE" in actions and "log yedeği" in actions


def test_full_recovery_with_fresh_log_backups_is_fine():
    assessment = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=1)],
        _probe(
            checked=["msdb"],
            found=["msdb"],
            recovery_models=[
                {
                    "database_name": "odeme",
                    "recovery_model": "FULL",
                    "last_log_at": (NOW - timedelta(minutes=20)).isoformat(),
                }
            ],
        ),
        now=NOW,
    )
    assert not any(k.startswith("recovery_model_mismatch") for k in _keys(assessment))


def test_simple_recovery_is_never_flagged_for_missing_log_backups():
    assessment = assess_instance(
        _instance("sqlserver"),
        [_record(started_hours_ago=1)],
        _probe(
            checked=["msdb"],
            found=["msdb"],
            recovery_models=[{"database_name": "rapor", "recovery_model": "SIMPLE", "last_log_at": None}],
        ),
        now=NOW,
    )
    assert not any(k.startswith("recovery_model_mismatch") for k in _keys(assessment))


def test_always_on_backup_replica_is_recorded():
    assessment = assess_instance(
        _instance("sqlserver"),
        [
            _record(started_hours_ago=1, detail={"server_name": "NODE1"}),
            _record(started_hours_ago=25, detail={"server_name": "NODE2"}),
        ],
        _probe(checked=["msdb"], found=["msdb"]),
        now=NOW,
    )
    assert assessment.backup_servers == ["NODE1", "NODE2"]


# --- Öneri standardı ----------------------------------------------------------------------


def _all_issue_advice():
    scenarios = [
        assess_instance(_instance("sqlserver"), [], _probe(checked=["msdb"]), now=NOW),
        assess_instance(
            _instance("sqlserver"),
            [_record(started_hours_ago=24 * 20), _record(backup_type=BackupType.LOG, started_hours_ago=10)],
            _probe(checked=["msdb"], found=["msdb"]),
            now=NOW,
        ),
        assess_instance(
            _instance("sqlserver"),
            [_record(status=BackupStatus.FAILED, started_hours_ago=1), _record(started_hours_ago=26)],
            _probe(checked=["msdb"], found=["msdb"]),
            now=NOW,
        ),
        assess_instance(
            _instance("sqlserver"),
            _duration_records(DURATION_ANOMALY_MIN_SECONDS * 4, DURATION_ANOMALY_MIN_SECONDS),
            _probe(checked=["msdb"], found=["msdb"]),
            now=NOW,
        ),
        assess_instance(
            _instance("sqlserver"),
            _sized_records(latest=10.0, baseline=100.0),
            _probe(checked=["msdb"], found=["msdb"]),
            now=NOW,
        ),
        assess_instance(
            _instance("postgresql"),
            [_record(source=BackupSource.PGBACKREST, started_hours_ago=1)],
            _probe(
                archiver={"archive_mode": "off"},
                slots=[{"slot_name": "s1", "retained_bytes": SLOT_RETAINED_WAL_CRITICAL_BYTES + 1}],
            ),
            now=NOW,
        ),
        assess_instance(
            _instance("sqlserver"),
            [_record(started_hours_ago=1)],
            _probe(
                checked=["msdb"],
                found=["msdb"],
                recovery_models=[{"database_name": "odeme", "recovery_model": "FULL", "last_log_at": None}],
            ),
            now=NOW,
        ),
    ]
    return [issue for a in scenarios for issue in a.issues]


def test_every_backup_issue_carries_a_five_part_advice():
    """CLAUDE.md: öneri neden + numaralı adımlar + komut + dikkat + doğrulama taşımalı."""
    issues = _all_issue_advice()
    assert len(issues) >= 8
    for issue in issues:
        advice = issue.advice
        assert advice.why, issue.key
        assert advice.steps, issue.key
        assert advice.cautions, issue.key
        assert advice.verification, issue.key


def test_every_backup_issue_carries_evidence():
    for issue in _all_issue_advice():
        assert issue.evidence.get("metric"), issue.key
        assert issue.evidence.get("measured_at"), issue.key


# --- Yönetici raporu ----------------------------------------------------------------------


def _sections(rows: list[dict]) -> dict:
    return {"items": {"backup": {"data": {"instances": rows}}}}


def test_executive_backup_uses_the_oldest_not_the_average():
    """Dokuz veritabanı dün, biri 40 gün önce yedeklendiyse ortalama "4 gün" der ve gerçek
    riski gizler."""
    rows = [{"sla_ok": True, "last_full_age_hours": 24.0} for _ in range(9)]
    rows.append({"sla_ok": False, "last_full_age_hours": 24 * 40})
    summary = _backup_from_sections(_sections(rows))
    assert summary["oldest_backup_days"] == pytest.approx(40.0)
    assert summary["sla_ok"] is False


def test_executive_backup_reports_unknown_without_claiming_a_breach():
    rows = [{"sla_ok": None, "last_full_age_hours": None}]
    summary = _backup_from_sections(_sections(rows))
    assert summary["sla_ok"] is None
    assert "anlamına gelmez" in summary["statement"]


def test_executive_backup_says_it_is_compliant_when_it_is():
    rows = [{"sla_ok": True, "last_full_age_hours": 20.0}]
    summary = _backup_from_sections(_sections(rows))
    assert summary["sla_ok"] is True
    assert "hedeflenen sıklıkta" in summary["statement"]


def test_executive_backup_statement_never_leaks_technical_detail():
    """Yönetici raporunda sorgu, parametre, komut, host geçemez (CLAUDE.md kuralı)."""
    for rows in (
        [{"sla_ok": False, "last_full_age_hours": 24 * 12}],
        [{"sla_ok": None, "last_full_age_hours": None}],
        [{"sla_ok": True, "last_full_age_hours": 3.0}],
        [],
    ):
        assert_no_technical_leak(_backup_from_sections(_sections(rows))["statement"], "test")


def test_missing_backup_age_is_shown_as_unknown_not_as_zero():
    assert _fmt_backup_age(None) == "—"
    assert "gün önce" in _fmt_backup_age(24 * 3)


# --- Rapor bölümü (uçtan uca) --------------------------------------------------------------


async def _db_instance(session, **over) -> Instance:
    base = dict(
        name=f"bkp-{uuid.uuid4().hex[:8]}", engine="sqlserver", host="h", port=1433,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _ctx(session, instances) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(days=1), period_end=end, previous=None, previous_findings={},
    )


async def test_report_section_produces_a_finding_for_a_stale_backup():
    async with SessionLocal() as session:
        instance = await _db_instance(session)
        session.add(
            BackupRecord(
                instance_id=instance.id, backup_type="full", source="msdb",
                external_id="1", started_at=datetime.now(UTC) - timedelta(days=30),
                finished_at=datetime.now(UTC) - timedelta(days=30), status="success",
            )
        )
        session.add(
            BackupProbe(
                instance_id=instance.id, probed_at=datetime.now(UTC),
                methods_checked=["msdb"], methods_found=["msdb"], errors=None,
            )
        )
        await session.commit()

        result = await backup_section(_ctx(session, [instance]))
        assert result.status == "critical"
        assert any("Tam yedek" in f.title for f in result.findings)
        row = result.data["instances"][0]
        assert row["sla_ok"] is False


async def test_report_section_does_not_call_an_unmonitored_instance_healthy():
    """Sonda hiç çalışmamışsa bölüm "sorunsuz" DEMEZ."""
    async with SessionLocal() as session:
        instance = await _db_instance(session)
        result = await backup_section(_ctx(session, [instance]))
        assert result.status == "unknown"
        assert result.unknown_reason


async def test_report_section_reports_disabled_monitoring_as_unknown_not_ok():
    from app.config import settings

    original = settings.backup_monitoring_enabled
    settings.backup_monitoring_enabled = False
    try:
        async with SessionLocal() as session:
            instance = await _db_instance(session)
            result = await backup_section(_ctx(session, [instance]))
            assert result.status == "unknown"
            assert "GELMEZ" in (result.unknown_reason or "")
    finally:
        settings.backup_monitoring_enabled = original


async def test_report_section_skips_mongodb_without_pretending_to_measure_it():
    async with SessionLocal() as session:
        instance = await _db_instance(session, engine="mongodb", port=27017)
        result = await backup_section(_ctx(session, [instance]))
        assert result.status == "unknown"
        assert "MongoDB" in (result.unknown_reason or "")


async def test_report_section_findings_carry_instance_link_and_advice():
    async with SessionLocal() as session:
        instance = await _db_instance(session)
        session.add(
            BackupProbe(
                instance_id=instance.id, probed_at=datetime.now(UTC),
                methods_checked=["msdb"], methods_found=[], errors=None,
            )
        )
        await session.commit()
        result = await backup_section(_ctx(session, [instance]))
        finding = result.findings[0]
        assert finding.related_object_type == "instance"
        assert finding.related_object_id == instance.id
        assert finding.advice is not None and finding.advice.steps
