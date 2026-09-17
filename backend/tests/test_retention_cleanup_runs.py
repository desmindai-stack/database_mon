"""Saklama temizliği GERÇEKTEN siliyor mu (Faz 31 Commit 5, madde 6).

Canlıda slow_query_samples 329 MB / ~393 bin satır. Politika kodda var (varsayılan 30 gün,
her gece 03:00 cron) ama bugüne kadar hiçbir test `run_retention_cleanup`'ı ÇAĞIRMIYORDU — işin
zamanlayıcıya kayıtlı olduğu sınanıyordu, sildiği değil. Bu test gerçek (SQLite) veritabanında
pencere dışı satırın silindiğini, pencere içindekinin kaldığını ve son çalışma kaydının
yazıldığını gösteriyor. Canlıda çalışıp çalışmadığı: DEPLOY.md'deki salt okunur SQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import AppSetting, IndexAdviceOutcome, Instance, SlowQuerySample
from app.services.credentials import encrypt_secret
from app.services.retention import (
    RETENTION_LAST_DELETED_KEY,
    RETENTION_LAST_RUN_KEY,
    get_retention_days,
    run_retention_cleanup,
)


async def test_cleanup_deletes_rows_outside_the_window_and_records_the_run():
    await init_db()
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        days = await get_retention_days(session)
        instance = Instance(name=f"ret-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
                            database="d", username="u", password=encrypt_secret("x"))
        session.add(instance)
        await session.flush()
        old = SlowQuerySample(instance_id=instance.id, queryid="old", query="SELECT 1", calls=1, total_time_ms=1,
                              mean_time_ms=1, rows=1, collected_at=now - timedelta(days=days + 1))
        fresh = SlowQuerySample(instance_id=instance.id, queryid="new", query="SELECT 1", calls=1, total_time_ms=1,
                                mean_time_ms=1, rows=1, collected_at=now - timedelta(days=days - 1))
        old_outcome = IndexAdviceOutcome(instance_id=instance.id, query_fingerprint="f", table_name="public.t",
                                         index_columns=["a"], index_ddl="CREATE INDEX x ON t (a);",
                                         registered_at=now - timedelta(days=days + 1))
        session.add_all([old, fresh, old_outcome])
        await session.commit()
        ids = old.id, fresh.id, old_outcome.id

    deleted = await run_retention_cleanup()

    async with SessionLocal() as session:
        assert await session.get(SlowQuerySample, ids[0]) is None, "pencere dışı satır silinmedi"
        assert await session.get(SlowQuerySample, ids[1]) is not None, "pencere içindeki satır silindi"
        assert await session.get(IndexAdviceOutcome, ids[2]) is None
        settings = dict((await session.execute(
            select(AppSetting.key, AppSetting.value).where(AppSetting.key.in_([RETENTION_LAST_RUN_KEY, RETENTION_LAST_DELETED_KEY]))
        )).all())
    assert deleted >= 2
    assert int(settings[RETENTION_LAST_DELETED_KEY]) == deleted
    assert datetime.fromisoformat(settings[RETENTION_LAST_RUN_KEY]) >= now
