"""Yedek toplama (Faz 28 İŞ 1a).

dbace'te yedek izleme hiç yoktu. Bu dosyanın koruduğu asıl fikir:

**YEDEK ALINDIĞI VARSAYILMAZ.** Kayıt bulunamadığında "yedek yok" denmiyor — hangi
yöntemlere bakıldığı ve neyin engellediği kaydediliyor. "Yedek yok" ile "dbace bulamadı"
çok farklı iki şey: ikincisini birincisi gibi sunmak, gerçekten yedeği olan kurumu paniğe,
olmayanı ise sahte güvene sürükler.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.domain.backups import (
    DEFAULT_THRESHOLDS,
    NOT_FOUND_GUIDANCE,
    BackupSource,
    BackupStatus,
    BackupType,
    thresholds_for,
)
from app.models import BackupProbe, BackupRecord, Instance
from app.services import backup_collection
from app.services.backup_tools import parse_barman, parse_pgbackrest, parse_tool_output, parse_wal_g
from app.services.credentials import encrypt_secret

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(engine: str = "postgresql", **over) -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"bkp-{uuid.uuid4().hex[:8]}", engine=engine, host="h",
            database="d", username="u", password=encrypt_secret("x"),
            **{"port": 5432, **over},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


class FakeCollector:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def collect_backups(self):
        return self.payload


# --- Eşikler ------------------------------------------------------------------------------


def test_default_thresholds_cover_every_backup_type():
    """Eşiği olmayan bir tür sessizce hiç uyarı üretmez — en tehlikeli hata biçimi."""
    for backup_type in BackupType:
        assert str(backup_type) in {str(k) for k in DEFAULT_THRESHOLDS}


def test_log_threshold_is_much_tighter_than_full():
    """Log yedeği kurtarma noktası hedefini (RPO) doğrudan belirliyor: bir saatlik log
    yedeği en fazla bir saatlik veri kaybı demek."""
    assert DEFAULT_THRESHOLDS[BackupType.LOG].warning_hours < (
        DEFAULT_THRESHOLDS[BackupType.FULL].warning_hours
    )


def test_thresholds_can_be_overridden_per_instance():
    """Yedek politikası veritabanına göre değişiyor: raporlama veritabanı için günlük tam
    yedek yeterliyken ödeme sistemi için 15 dakikalık log bile gevşek kalabilir."""
    resolved = thresholds_for({"backup_thresholds": {"log": {"warning_hours": 0.25}}})
    assert resolved["log"].warning_hours == 0.25
    # Dokunulmayanlar varsayılandan geliyor.
    assert resolved["full"].warning_hours == DEFAULT_THRESHOLDS[BackupType.FULL].warning_hours


def test_a_critical_threshold_below_warning_is_corrected():
    """Yanlış yapılandırmayı sessizce kabul etmek, kritik uyarının HİÇ tetiklenmemesi
    demekti."""
    resolved = thresholds_for(
        {"backup_thresholds": {"full": {"warning_hours": 48, "critical_hours": 12}}}
    )
    assert resolved["full"].critical_hours >= resolved["full"].warning_hours


def test_garbage_overrides_fall_back_instead_of_crashing():
    resolved = thresholds_for({"backup_thresholds": {"full": {"warning_hours": "çok"}}})
    assert resolved["full"].warning_hours == DEFAULT_THRESHOLDS[BackupType.FULL].warning_hours


def test_every_engine_has_configuration_guidance():
    """"Yedek bulunamadı" demek yetmez: kullanıcı dbace'in NEREYE baktığını bilmeli."""
    for engine in ("postgresql", "sqlserver", "mongodb"):
        assert len(NOT_FOUND_GUIDANCE[engine]) > 100


# --- pgBackRest ayrıştırma ------------------------------------------------------------------


PGBACKREST_JSON = json.dumps(
    [
        {
            "name": "main",
            "backup": [
                {
                    "label": "20260913-030000F",
                    "type": "full",
                    "timestamp": {"start": 1789261200, "stop": 1789262400},
                    "info": {"size": 5_000_000_000, "repository": {"size": 1_200_000_000}},
                },
                {
                    "label": "20260914-030000I",
                    "type": "incr",
                    "timestamp": {"start": 1789347600, "stop": 1789347900},
                    "info": {"size": 100_000_000},
                },
            ],
        }
    ]
)


def test_pgbackrest_json_is_parsed():
    records = parse_pgbackrest(PGBACKREST_JSON)
    assert len(records) == 2
    full = records[0]
    assert full["backup_type"] == BackupType.FULL
    assert full["duration_seconds"] == 1200.0
    assert full["size_bytes"] == 5_000_000_000
    assert full["source"] == BackupSource.PGBACKREST


def test_incremental_maps_to_differential_but_keeps_the_raw_type():
    """`incr` ve `diff` ikisi de "tam olmayan ara yedek" ve yaş eşiği açısından aynı
    davranıyor; ayrım kaybolmasın diye ham tür detayda saklanıyor."""
    records = parse_pgbackrest(PGBACKREST_JSON)
    incremental = records[1]
    assert incremental["backup_type"] == BackupType.DIFFERENTIAL
    assert incremental["detail"]["raw_type"] == "incr"


def test_a_failed_pgbackrest_backup_is_marked_failed():
    payload = json.dumps(
        [{"name": "main", "backup": [
            {"label": "x", "type": "full", "error": True,
             "timestamp": {"start": 1789261200, "stop": 1789262400}}
        ]}]
    )
    assert parse_pgbackrest(payload)[0]["status"] == BackupStatus.FAILED


def test_an_unfinished_pgbackrest_backup_is_running():
    payload = json.dumps(
        [{"name": "main", "backup": [
            {"label": "x", "type": "full", "timestamp": {"start": 1789261200}}
        ]}]
    )
    assert parse_pgbackrest(payload)[0]["status"] == BackupStatus.RUNNING


# --- WAL-G ---------------------------------------------------------------------------------


def test_wal_g_json_is_parsed():
    payload = json.dumps([{"backup_name": "base_0001", "time": "2026-09-14T03:14:07Z"}])
    records = parse_wal_g(payload)
    assert len(records) == 1
    assert records[0]["external_id"] == "base_0001"
    assert records[0]["backup_type"] == BackupType.BASE_BACKUP


def test_wal_g_text_output_is_parsed_when_json_is_unsupported():
    """WAL-G'nin JSON bayrağı sürüme bağlı; eski sürümlerde bayrak yok sayılıp metin
    basılıyor. Yalnızca JSON beklemek, o sürümlerde yedeği görünmez kılardı."""
    text = "base_000000010000000000000002  2026-09-14T03:14:07Z  wal_segment_backup_start"
    records = parse_wal_g(text)
    assert len(records) == 1
    assert records[0]["external_id"].startswith("base_")


def test_wal_g_does_not_invent_a_duration():
    """WAL-G listesi bitiş zamanı vermiyor; süre BİLİNMİYOR ve uydurulmuyor."""
    payload = json.dumps([{"backup_name": "b", "time": "2026-09-14T03:14:07Z"}])
    assert parse_wal_g(payload)[0]["duration_seconds"] is None


# --- Barman --------------------------------------------------------------------------------


def test_barman_text_output_is_parsed():
    text = "pg-main 20260914T031407 - Sat Sep 14 03:14:07 2026 - Size: 1.2 GiB - WAL Size: 100 MiB"
    records = parse_barman(text)
    assert len(records) == 1
    assert records[0]["external_id"] == "pg-main:20260914T031407"
    assert records[0]["size_bytes"] == pytest.approx(1.2 * 1024**3)


def test_a_failed_barman_backup_is_marked():
    text = "pg-main 20260914T031407 - Sat Sep 14 03:14:07 2026 - FAILED"
    assert parse_barman(text)[0]["status"] == BackupStatus.FAILED


def test_unparsable_barman_output_yields_nothing_rather_than_a_wrong_date():
    """Yanlış ayrıştırılmış bir tarih "yedek 40 gün eski" gibi SAHTE bir kritik bulgu
    üretirdi — bu, hiç göstermemekten kötü."""
    assert parse_barman("bambaşka bir çıktı biçimi\nikinci satır") == []


# --- Araç kurulu değilse ---------------------------------------------------------------------


def test_a_tool_that_is_not_installed_yields_no_records_and_is_not_an_error():
    """Çoğu kurulumda üç araçtan yalnızca biri var; diğerlerinin "bulunamadı" demesi normal.
    Hata olarak raporlamak kullanıcıyı olmayan bir sorunu aramaya yollardı."""
    assert parse_tool_output("pgbackrest", {"installed": False, "output": ""}) == []


def test_empty_output_is_handled():
    assert parse_tool_output("pgbackrest", {"installed": True, "output": "   "}) == []


def test_broken_output_does_not_raise():
    assert parse_tool_output("pgbackrest", {"installed": True, "output": "{bozuk json"}) == []


# --- Saklama ---------------------------------------------------------------------------------


async def test_backup_records_are_written_and_deduplicated():
    """Aynı yedek her sondada yeniden görülüyor (msdb geçmişi kalıcı, pgBackRest listesi
    kalıcı); tekrar yazmak tabloyu şişirirdi."""
    instance = await _instance()
    records = parse_pgbackrest(PGBACKREST_JSON)

    async with SessionLocal() as session:
        first = await backup_collection.store_backup_records(session, instance.id, records)
        await session.commit()
    async with SessionLocal() as session:
        second = await backup_collection.store_backup_records(session, instance.id, records)
        await session.commit()
        rows = (
            await session.execute(
                select(BackupRecord).where(BackupRecord.instance_id == instance.id)
            )
        ).scalars().all()

    assert first == 2
    assert second == 0
    assert len(rows) == 2


async def test_a_running_backup_is_updated_when_it_finishes():
    """Yalnızca "yoksa ekle" yapsaydık devam eden bir yedek sonsuza kadar "sürüyor"
    görünürdü ve süre anomalisi mantığı çöpe giderdi."""
    instance = await _instance()
    running = [{
        "external_id": "b1", "backup_type": "full", "source": "pgbackrest",
        "started_at": NOW.isoformat(), "finished_at": None, "status": "running",
    }]
    finished = [{
        "external_id": "b1", "backup_type": "full", "source": "pgbackrest",
        "started_at": NOW.isoformat(), "finished_at": (NOW + timedelta(minutes=20)).isoformat(),
        "duration_seconds": 1200.0, "status": "success",
    }]

    async with SessionLocal() as session:
        await backup_collection.store_backup_records(session, instance.id, running)
        await session.commit()
    async with SessionLocal() as session:
        await backup_collection.store_backup_records(session, instance.id, finished)
        await session.commit()
        row = (
            await session.execute(
                select(BackupRecord).where(BackupRecord.instance_id == instance.id)
            )
        ).scalar_one()

    assert row.status == "success"
    assert row.finished_at is not None
    assert row.duration_seconds == 1200.0


async def test_a_record_without_a_start_time_is_skipped():
    """Başlangıcı olmayan bir kayıttan yaş hesaplanamaz; yazmak boş bir satır üretirdi."""
    instance = await _instance()
    async with SessionLocal() as session:
        written = await backup_collection.store_backup_records(
            session, instance.id, [{"external_id": "x", "source": "pgbackrest"}]
        )
        await session.commit()
    assert written == 0


# --- Sonda kaydı ------------------------------------------------------------------------------


async def test_the_probe_records_which_methods_were_tried(monkeypatch):
    """SONDANIN KENDİSİ KAYDEDİLİYOR: "yedek kaydı yok" ile "yedek arayamadık" farklı
    şeyler ve ayırt edebilmek için ne denendiğini bilmek gerekiyor."""
    instance = await _instance()
    monkeypatch.setattr(
        backup_collection, "get_collector",
        lambda engine, target: FakeCollector(
            {"archiver": None, "slots": [], "running": [], "records": [], "errors": {}}
        ),
    )
    async with SessionLocal() as session:
        outcome = await backup_collection.probe_instance(session, instance)
        await session.commit()
        probe = (
            await session.execute(
                select(BackupProbe).where(BackupProbe.instance_id == instance.id)
            )
        ).scalar_one()

    assert "pg_stat_archiver" in probe.methods_checked
    assert probe.methods_found == []
    # Agent yoksa harici araçların GÖRÜLEMEYECEĞİ açıkça söyleniyor.
    assert "backup_tools" in (probe.errors or {})
    assert "GÖREMEZ" in probe.errors["backup_tools"]
    assert outcome["records"] == 0


async def test_a_probe_with_archiver_data_marks_the_method_as_found(monkeypatch):
    instance = await _instance()
    monkeypatch.setattr(
        backup_collection, "get_collector",
        lambda engine, target: FakeCollector({
            "archiver": {
                "archive_mode": "on", "archived_count": 500,
                "last_archived_time": NOW.isoformat(), "failed_count": 0,
            },
            "slots": [], "running": [], "records": [], "errors": {},
        }),
    )
    async with SessionLocal() as session:
        await backup_collection.probe_instance(session, instance)
        await session.commit()
        probe = (
            await session.execute(
                select(BackupProbe).where(BackupProbe.instance_id == instance.id)
            )
        ).scalar_one()
    assert "pg_stat_archiver" in probe.methods_found
    assert probe.archiver["archive_mode"] == "on"


async def test_a_running_basebackup_becomes_a_record(monkeypatch):
    """"Devam eden yedek tespiti ve süresi" ayrı bir gereksinim: süresi uzayan bir yedek,
    biten bir yedekten daha acil bir işaret."""
    instance = await _instance()
    monkeypatch.setattr(
        backup_collection, "get_collector",
        lambda engine, target: FakeCollector({
            "archiver": None, "slots": [], "records": [], "errors": {},
            "running": [{"pid": 42, "phase": "streaming database files", "elapsed_seconds": 3600.0}],
        }),
    )
    async with SessionLocal() as session:
        await backup_collection.probe_instance(session, instance)
        await session.commit()
        row = (
            await session.execute(
                select(BackupRecord).where(BackupRecord.instance_id == instance.id)
            )
        ).scalar_one()
    assert row.status == "running"
    assert row.finished_at is None
    assert row.duration_seconds == 3600.0


async def test_mongodb_is_recorded_as_unsupported_rather_than_skipped():
    """Sessizce atlamak, arayüzde "yedek yok" gibi görünürdü."""
    instance = await _instance(engine="mongodb", port=27017)
    async with SessionLocal() as session:
        await backup_collection.probe_instance(session, instance)
        await session.commit()
        probe = (
            await session.execute(
                select(BackupProbe).where(BackupProbe.instance_id == instance.id)
            )
        ).scalar_one()
    assert "mongodb" in (probe.errors or {})


async def test_a_collector_failure_is_recorded_not_swallowed(monkeypatch):
    """Yetki hatası "yedek yok" gibi görünmemeli."""
    instance = await _instance()

    class Failing:
        async def collect_backups(self):
            raise RuntimeError("msdb üzerinde SELECT yetkisi yok")

    monkeypatch.setattr(backup_collection, "get_collector", lambda engine, target: Failing())
    async with SessionLocal() as session:
        await backup_collection.probe_instance(session, instance)
        await session.commit()
        probe = (
            await session.execute(
                select(BackupProbe).where(BackupProbe.instance_id == instance.id)
            )
        ).scalar_one()
    assert probe.methods_found == []
    assert any("yetkisi yok" in v for v in (probe.errors or {}).values())


# --- Saklama penceresi -------------------------------------------------------------------


def test_backup_retention_is_longer_than_the_general_window():
    """SAKLAMA POLİTİKASI İZLEMEYİ BOZMAMALI. Genel pencere 7 güne inebiliyor; haftalık tam
    yedek alan bir kurumda bu, EN SON yedeği silip "hiç yedek bulunamadı" sonucunu üretirdi."""
    from app.services.retention import ALLOWED_RETENTION_DAYS, BACKUP_MIN_RETENTION_DAYS

    assert BACKUP_MIN_RETENTION_DAYS > max(
        DEFAULT_THRESHOLDS[BackupType.FULL].critical_hours / 24,
        min(ALLOWED_RETENTION_DAYS),
    )
