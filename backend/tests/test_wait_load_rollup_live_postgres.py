"""Saatlik toplulaştırma — GERÇEK PostgreSQL meta veritabanında (Faz 31 Commit 10c-C).

SQLite ile kanıtlanamayan iki şey burada sınanıyor:
1. PostgreSQL'in epoch bölme lehçesi (`extract('epoch', ...)`) gerçekten SAAT sınırına gruplyor.
2. `INSERT .. ON CONFLICT DO UPDATE` biriktirme (additive upsert) gerçek Postgres'te de doğru toplanıyor —
   saat sınırını iki ayrı retention turuna bölen senaryo (SQLite testinde de var, burada gerçek sunucuda).

dbace'in KENDİ (meta) şeması, `scripts/live_pg.py up`'ın kurduğu standart hedeflerden birinde ayrı bir veritabanında
kuruluyor (`test_migration_scale_live_postgres.py`'deki ile AYNI desen); `SessionLocal()` yerine buraya bağlı KENDİ
session fabrikamızı `ensure_wait_load_rollup`/`build_database_load`'a açıkça veriyoruz — global `app.database.engine`'i
değiştirmeden. Engine test fonksiyonunun İÇİNDE kuruluyor (modül ölçekli bir fixture değil): pytest-asyncio her testi
kendi olay döngüsünde çalıştırıyor, engine'i döngüler arasında paylaşmak asyncpg'de "attached to a different loop"
hatası veriyordu.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.live_pg import LIVE_DSNS, SKIP_REASON, with_database

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

DATABASE = "dbace_wait_rollup_it"


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


async def test_real_postgres_hour_bucketing_and_additive_upsert(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import models  # noqa: F401 — Base.metadata'yı doldurur
    from app.config import settings
    from app.database import Base
    from app.models import (
        ActiveSessionMinute,
        ActiveSessionRollupHourly,
        Instance,
        WaitLoadRollupHourly,
        WaitSampleMinute,
    )
    from app.services.credentials import encrypt_secret
    from app.services.database_load import build_database_load
    from app.services.wait_load_rollup import ensure_wait_load_rollup

    admin = await asyncpg.connect(with_database(LIVE_DSNS[0], "postgres"), statement_cache_size=0)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {DATABASE}")
    finally:
        await admin.close()

    postgres_url = with_database(LIVE_DSNS[0], DATABASE)
    async_url = postgres_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # `_epoch_bucket` (database_load.py) lehçeyi GLOBAL `settings.database_url`den okuyor (tek süreç = tek meta
    # veritabanı varsayımı); bu test farklı bir engine kullandığı için elle Postgres'e çevriliyor — aksi hâlde
    # SQLite dalı (`strftime`) seçilir ve gerçek Postgres'te "function strftime does not exist" ile patlar.
    monkeypatch.setattr(settings, "database_url", postgres_url)
    engine = create_async_engine(async_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        now = datetime.now(UTC)
        async with session_factory() as session:
            instance = Instance(name="rollup-pg-it", engine="postgresql", host="h", port=5432, database="d",
                                username="u", password=encrypt_secret("x"))
            session.add(instance)
            await session.commit()
            await session.refresh(instance)
            instance_id = instance.id

        hour = (now - timedelta(days=10)).replace(minute=0, second=0, microsecond=0)

        # 12 farklı dakika (5'er dakika arayla), TEK saate düşüyor — gerçek Postgres'te GRUPLANMALI.
        async with session_factory() as session:
            for offset in range(0, 60, 5):
                session.add(ActiveSessionMinute(instance_id=instance_id, minute=hour + timedelta(minutes=offset),
                                                samples_taken=10, active_sessions_sampled=5,
                                                blocked_sessions_sampled=0))
                session.add(WaitSampleMinute(instance_id=instance_id, minute=hour + timedelta(minutes=offset),
                                             queryid="q1", wait_category="cpu", wait_event="", sample_count=4))
            await session.commit()

        # 1. tur: cutoff saatin TAM ortasından geçiyor — additive upsert'in ikinci turda BİRİKMESİ gerekiyor.
        async with session_factory() as session:
            first = await ensure_wait_load_rollup(session, before=hour + timedelta(minutes=30))
            await session.commit()
        log("1. tur (saatin yarısı)", first)
        async with session_factory() as session:
            after_first = (await session.execute(select(ActiveSessionRollupHourly))).scalar_one()
        assert after_first.samples_taken == 60, "yalnızca ilk 30 dakika (6 satır x 10) işlenmeliydi"

        # 2. tur: geri kalan da pencerenin dışına çıktı.
        async with session_factory() as session:
            second = await ensure_wait_load_rollup(session, before=now - timedelta(days=7))
            await session.commit()
        log("2. tur (kalan yarısı)", second)

        async with session_factory() as session:
            active_row = (await session.execute(select(ActiveSessionRollupHourly))).scalar_one()
            wait_row = (await session.execute(select(WaitLoadRollupHourly))).scalar_one()
            remaining_raw = (await session.execute(select(ActiveSessionMinute))).scalars().all()
        log("saatlik toplam (biriken)", {"samples_taken": active_row.samples_taken,
                                         "sample_count": wait_row.sample_count})
        assert active_row.samples_taken == 120, "iki turun toplamı BİRİKMELİYDİ (12 x 10), üzerine yazılmamalıydı"
        assert wait_row.sample_count == 48, "12 x 4"
        assert remaining_raw == [], "ham satırların tamamı silinmeliydi"

        # database_load.py'nin rollup yolu GERÇEK Postgres'ten doğru okuyor mu.
        async with session_factory() as session:
            instance = await session.get(Instance, instance_id)
            report = await build_database_load(session, instance, start=hour, end=hour + timedelta(hours=1))
        log("rollup yolu raporu", {"source": report.source, "samples_taken": report.samples_taken,
                                   "average_aas": report.average_aas})
        assert report.source == "rollup"
        assert report.samples_taken == 120
        assert report.average_aas == pytest.approx(48 / 120)
    finally:
        await engine.dispose()
        admin = await asyncpg.connect(with_database(LIVE_DSNS[0], "postgres"), statement_cache_size=0)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        finally:
            await admin.close()
