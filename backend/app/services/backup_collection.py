"""Yedek bilgisinin toplanması ve saklanması (Faz 28 İŞ 1).

Kaynaklar motora göre bambaşka ve tek bir yerden okunamıyor:

| Motor | Kaynak | Nereden |
|---|---|---|
| SQL Server | `msdb.dbo.backupset` | Veritabanının kendisi |
| PostgreSQL | `pg_stat_archiver`, replikasyon slotları, devam eden taban yedek | Veritabanının kendisi |
| PostgreSQL | pgBackRest / Barman / WAL-G | Host-agent üzerinden araç çıktısı |

Bu modül hepsini toplayıp ortak tabloya yazıyor ve **ne aradığını kaydediyor**.

EN ÖNEMLİ KURAL: yedek alındığı VARSAYILMAZ. Kayıt bulunamadığında "yedek yok" denmiyor;
hangi yöntemlere bakıldığı ve nasıl yapılandırılacağı yazılıyor. Bir izleme aracının
"yedeğiniz var" diye yanlış güvence vermesi, hiç bilgi vermemekten çok daha tehlikeli.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.registry import get_collector
from app.config import settings
from app.database import SessionLocal
from app.domain.backups import BackupSource, BackupStatus
from app.domain.engines import DatabaseEngine
from app.models import BackupProbe, BackupRecord, Instance
from app.services.backup_tools import TOOLS, fetch_tool_output, parse_tool_output
from app.services.collection import connection_target_for

logger = logging.getLogger(__name__)

#: Yedek kayıtları sık değişmiyor: en agresif politikada bile saatte bir log yedeği alınıyor.
#: 15 dakika, bir log yedeğinin gecikmesini fark etmeye yeter ve msdb'ye gereksiz yük bindirmez.
DEFAULT_INTERVAL_SECONDS = 900


def _agent_configured(instance: Instance) -> bool:
    return bool((instance.options or {}).get("agent_url"))


def _parse_dt(value) -> datetime | None:
    """ISO metin → UTC datetime.

    SAAT DİLİMİ VARSAYIMI: msdb zaman damgaları sunucunun YEREL saatinde ve saat dilimi
    bilgisi taşımıyor. Naive bir değeri UTC saymak, sunucu UTC'de değilse yaş hesabını saat
    farkı kadar kaydırır. Bunu düzeltmenin tek dürüst yolu sunucunun saat dilimini de okumak
    olurdu; şimdilik varsayım BURADA ve tek yerde duruyor (bkz. SORULAR.md).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def store_backup_records(
    session: AsyncSession, instance_id: int, records: list[dict]
) -> int:
    """Kayıtları yazar; var olanı GÜNCELLER.

    Neden güncelleme: devam eden bir yedek (`finished_at` boş) bir sonraki sondada bitmiş
    olacak. Yalnızca "yoksa ekle" yapsaydık, o yedek sonsuza kadar "devam ediyor" görünürdü
    ve süre anomalisi mantığı çöpe giderdi.
    """
    written = 0
    for record in records:
        external_id = str(record.get("external_id") or "")
        source = str(record.get("source") or "")
        started_at = _parse_dt(record.get("started_at"))
        if not external_id or started_at is None:
            continue

        existing = (
            await session.execute(
                select(BackupRecord).where(
                    BackupRecord.instance_id == instance_id,
                    BackupRecord.source == source,
                    BackupRecord.external_id == external_id,
                )
            )
        ).scalar_one_or_none()

        finished_at = _parse_dt(record.get("finished_at"))
        values = {
            "backup_type": str(record.get("backup_type") or "full"),
            "database_name": record.get("database_name"),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": record.get("duration_seconds"),
            "size_bytes": record.get("size_bytes"),
            "status": str(record.get("status") or BackupStatus.SUCCESS),
            "error_message": record.get("error_message"),
            "detail": record.get("detail"),
        }
        if existing is None:
            session.add(
                BackupRecord(
                    instance_id=instance_id, source=source, external_id=external_id, **values
                )
            )
            written += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
    return written


async def probe_instance(session: AsyncSession, instance: Instance) -> dict:
    """Bir instance için tüm yedek kaynaklarını yoklar ve sonucu yazar."""
    engine = instance.engine
    outcome = {
        "records": 0,
        "methods_checked": [],
        "methods_found": [],
        "errors": {},
    }
    if engine == str(DatabaseEngine.MONGODB):
        # Sessizce atlamıyoruz: sonda kaydı yazılıyor ki arayüz "desteklenmiyor" diyebilsin.
        outcome["errors"]["mongodb"] = "MongoDB için yedek izleme desteklenmiyor."
        await _write_probe(session, instance.id, outcome, archiver=None, slots=[])
        return outcome

    collector = get_collector(DatabaseEngine(engine), connection_target_for(instance))
    archiver = None
    slots: list = []
    records: list[dict] = []

    # --- Veritabanının kendi içinden ---
    in_db_method = (
        BackupSource.MSDB if engine == str(DatabaseEngine.SQLSERVER)
        else BackupSource.PG_STAT_ARCHIVER
    )
    outcome["methods_checked"].append(str(in_db_method))
    try:
        payload = await collector.collect_backups()
        archiver = payload.get("archiver")
        slots = payload.get("slots") or []
        records.extend(payload.get("records") or [])
        outcome["errors"].update(payload.get("errors") or {})

        # Devam eden taban yedek de bir kayıt: "devam eden yedek tespiti ve süresi" isteği.
        for running in payload.get("running") or []:
            started = datetime.now(UTC)
            elapsed = float(running.get("elapsed_seconds") or 0)
            records.append(
                {
                    "external_id": f"basebackup:{running.get('pid')}",
                    "backup_type": "base_backup",
                    "started_at": (
                        datetime.fromtimestamp(started.timestamp() - elapsed, tz=UTC).isoformat()
                    ),
                    "finished_at": None,
                    "duration_seconds": elapsed,
                    "status": BackupStatus.RUNNING,
                    "source": BackupSource.PG_BASEBACKUP,
                    "detail": running,
                }
            )

        found = bool(records) or bool(archiver and archiver.get("last_archived_time"))
        if found:
            outcome["methods_found"].append(str(in_db_method))
    except Exception as exc:
        outcome["errors"][str(in_db_method)] = str(exc)

    # --- Harici araçlar (yalnızca PostgreSQL, yalnızca agent varsa) ---
    if engine == str(DatabaseEngine.POSTGRESQL):
        if not _agent_configured(instance):
            # HATA DEĞİL, yapılandırma eksikliği — ve kullanıcının bilmesi gereken bir şey:
            # gerçek yedekleri pgBackRest alıyorsa dbace onları agent olmadan göremez.
            outcome["errors"]["backup_tools"] = (
                "Host-agent yapılandırılmamış (`agent_url`). pgBackRest / Barman / WAL-G "
                "durumu yalnızca agent üzerinden okunabiliyor; bu sunucuda harici yedek "
                "aracı kullanılıyorsa dbace onu GÖREMEZ."
            )
        else:
            for tool in TOOLS:
                outcome["methods_checked"].append(tool)
                try:
                    payload = await fetch_tool_output(instance.options or {}, tool)
                except Exception as exc:
                    outcome["errors"][tool] = str(exc)
                    continue
                if not payload.get("installed"):
                    # Araç kurulu değil: normal durum, hata olarak yazılmıyor.
                    continue
                if payload.get("error"):
                    outcome["errors"][tool] = str(payload["error"])
                    continue
                parsed = parse_tool_output(tool, payload)
                if parsed:
                    records.extend(parsed)
                    outcome["methods_found"].append(tool)

    outcome["records"] = await store_backup_records(session, instance.id, records)
    await _write_probe(session, instance.id, outcome, archiver=archiver, slots=slots)
    return outcome


async def _write_probe(
    session: AsyncSession, instance_id: int, outcome: dict, *, archiver, slots
) -> None:
    """Sonda sonucunu yazar (instance başına tek satır, üzerine yazılıyor)."""
    existing = (
        await session.execute(select(BackupProbe).where(BackupProbe.instance_id == instance_id))
    ).scalar_one_or_none()
    values = {
        "probed_at": datetime.now(UTC),
        "methods_checked": outcome["methods_checked"],
        "methods_found": outcome["methods_found"],
        "archiver": archiver,
        "slots": slots,
        "errors": outcome["errors"] or None,
    }
    if existing is None:
        session.add(BackupProbe(instance_id=instance_id, **values))
    else:
        for key, value in values.items():
            setattr(existing, key, value)


async def backup_collection_tick() -> dict:
    """Zamanlayıcı turu: yedek izleme açık olan tüm instance'lar."""
    if not settings.backup_monitoring_enabled:
        return {"instances": 0, "records": 0}
    totals = {"instances": 0, "records": 0}
    async with SessionLocal() as session:
        instances = (
            await session.execute(select(Instance).where(Instance.enabled.is_(True)))
        ).scalars().all()
        for instance in instances:
            if instance.engine == str(DatabaseEngine.MONGODB):
                continue
            totals["instances"] += 1
            try:
                outcome = await probe_instance(session, instance)
                totals["records"] += outcome["records"]
            except Exception:
                logger.exception("Yedek sondası başarısız (instance %s)", instance.name)
        await session.commit()
    return totals
