"""Bekleme yükünün SAATLİK toplulaştırması — ham veri kısa saklanırken eski dönemler buradan okunuyor
(Faz 31 Commit 10c-C).

## Sorun (ölçüldü, Faz 31 Commit 10a)

20 instance için `wait_sample_minutes` yazım hacmi ~10 MB/saat; 30 gün saklamada bu ~7 GB'a çıkıyor — Supabase
Pro'nun 8 GB'lık veritabanı kotasının neredeyse tamamı. Dakika dakika ayrıntı ("hangi sorgu, hangi bekleme türü")
yalnızca SON GÜNLERİN sorgu teşhisinde işe yarıyor; "geçen ay yükümüz nasıldı" sorusunun cevabı SAATLİK bir
ortalama ile aynı doğrulukta.

## Çözüm

Ham `wait_sample_minutes`/`active_session_minutes` artık `WAIT_LOAD_RAW_RETENTION_DAYS` (7) gün saklanıyor
(`services/retention.py`). Bu modül, o pencerenin DIŞINA çıkan satırları SİLİNMEDEN ÖNCE saatlik toplamlara
biriktirir (`ensure_wait_load_rollup`) — `services/database_load.py` 7 günden eski bir aralık istendiğinde
buradan okur.

**Neden queryid yok:** sorgu bazlı kırılım ham veride kalıyor; saatlik toplamda YOK — her instance için saatte
onlarca farklı queryid × kategori satırı, rollup'ın kazancını büyük ölçüde geri verirdi. 7 günden eski bir aralıkta
"yük üreten sorgular" listesi boş döner ve NEDENİ söylenir (bu bilinçli bir kapsam daralması, kanıtsız bulgu değil).

**Neden additive (`+=`) upsert, tek seferlik INSERT değil:** saat sınırındaki bir kova İKİ ayrı saklama turuna
bölünebilir (cutoff tam o saatin ortasından geçtiğinde: bir turda saatin İLK yarısı silinmeye hazır olur, ertesi
gün İKİNCİ yarısı). `ON CONFLICT DO UPDATE ... +=` ikisini de doğru toplar; `DO NOTHING` ikinci yarıyı sessizce
kaybederdi.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ActiveSessionMinute, ActiveSessionRollupHourly, WaitLoadRollupHourly, WaitSampleMinute
from app.services.database_load import _epoch_bucket

logger = logging.getLogger(__name__)

HOUR_SECONDS = 3600


@dataclass
class RollupResult:
    hours_rolled_up: int = 0
    active_session_rows_deleted: int = 0
    wait_sample_rows_deleted: int = 0


def _is_sqlite() -> bool:
    return settings.database_url.startswith("sqlite")


def _dialect_insert(model):
    return (sqlite_dialect if _is_sqlite() else pg_dialect).insert(model)


async def _rollup_active_session_minutes(session: AsyncSession, instance_id: int, before: datetime) -> int:
    bucket = _epoch_bucket(ActiveSessionMinute.minute, HOUR_SECONDS)
    rows = (
        await session.execute(
            select(
                bucket.label("hour_epoch"),
                func.sum(ActiveSessionMinute.samples_taken).label("samples_taken"),
                func.sum(ActiveSessionMinute.active_sessions_sampled).label("active_sessions_sampled"),
                func.sum(ActiveSessionMinute.blocked_sessions_sampled).label("blocked_sessions_sampled"),
            )
            .where(ActiveSessionMinute.instance_id == instance_id, ActiveSessionMinute.minute < before)
            .group_by(bucket)
        )
    ).all()
    if not rows:
        return 0
    table = ActiveSessionRollupHourly.__table__
    values = [{"instance_id": instance_id, "hour": datetime.fromtimestamp(int(r.hour_epoch), tz=UTC),
              "samples_taken": int(r.samples_taken or 0), "active_sessions_sampled": int(r.active_sessions_sampled or 0),
              "blocked_sessions_sampled": int(r.blocked_sessions_sampled or 0)} for r in rows]
    stmt = _dialect_insert(ActiveSessionRollupHourly).values(values)
    incoming = stmt.excluded
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.instance_id, table.c.hour],
        set_={
            "samples_taken": table.c.samples_taken + incoming.samples_taken,
            "active_sessions_sampled": table.c.active_sessions_sampled + incoming.active_sessions_sampled,
            "blocked_sessions_sampled": table.c.blocked_sessions_sampled + incoming.blocked_sessions_sampled,
        },
    ))
    return len(values)


async def _rollup_wait_sample_minutes(session: AsyncSession, instance_id: int, before: datetime) -> int:
    from app.domain.waits import is_load_bearing

    bucket = _epoch_bucket(WaitSampleMinute.minute, HOUR_SECONDS)
    rows = (
        await session.execute(
            select(bucket.label("hour_epoch"), WaitSampleMinute.wait_category,
                  func.sum(WaitSampleMinute.sample_count).label("sample_count"))
            .where(WaitSampleMinute.instance_id == instance_id, WaitSampleMinute.minute < before)
            .group_by(bucket, WaitSampleMinute.wait_category)
        )
    ).all()
    if not rows:
        return 0
    # Arka plan/boşta bekleme kategorileri yük değil (database_load.py'deki ile AYNI kural): rollup'a girmeleri
    # gerçek yükün önünde yalancı bir taban oluşturur, çünkü genel toplamda ayıklanamazlar.
    values = [{"instance_id": instance_id, "hour": datetime.fromtimestamp(int(r.hour_epoch), tz=UTC),
              "wait_category": r.wait_category, "sample_count": int(r.sample_count or 0)}
             for r in rows if is_load_bearing(r.wait_category)]
    if not values:
        return 0
    table = WaitLoadRollupHourly.__table__
    stmt = _dialect_insert(WaitLoadRollupHourly).values(values)
    incoming = stmt.excluded
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.instance_id, table.c.hour, table.c.wait_category],
        set_={"sample_count": table.c.sample_count + incoming.sample_count},
    ))
    return len(values)


async def ensure_wait_load_rollup(session: AsyncSession, *, before: datetime) -> RollupResult:
    """`before`'dan ÖNCEKİ ham satırları saatlik toplamlara biriktirir ve SONRA siler.

    Instance başına ayrı sorgu: tek sorguda tüm instance'ları GROUP BY'lamak, bir instance'ın 400 gün birikmiş
    verisiyle (yeni bir dağıtımın ilk çalıştırması) diğerlerinin dakikalık tazeliğini aynı sorguya bağlardı; ayrı
    sorgular sırayla ilerler ve tek bir yavaş instance diğerlerini geciktirmez.
    """
    result = RollupResult()
    instance_ids = [row[0] for row in (await session.execute(
        select(ActiveSessionMinute.instance_id).where(ActiveSessionMinute.minute < before).distinct()
    )).all()]
    for instance_id in instance_ids:
        result.hours_rolled_up += await _rollup_active_session_minutes(session, instance_id, before)
        await _rollup_wait_sample_minutes(session, instance_id, before)

    active_deleted = await session.execute(delete(ActiveSessionMinute).where(ActiveSessionMinute.minute < before))
    wait_deleted = await session.execute(delete(WaitSampleMinute).where(WaitSampleMinute.minute < before))
    result.active_session_rows_deleted = active_deleted.rowcount or 0
    result.wait_sample_rows_deleted = wait_deleted.rowcount or 0
    if result.hours_rolled_up or result.active_session_rows_deleted:
        logger.info("Bekleme yükü toplulaştırması: %s instance, %s saat, %s+%s ham satır silindi",
                    len(instance_ids), result.hours_rolled_up, result.active_session_rows_deleted,
                    result.wait_sample_rows_deleted)
    return result
