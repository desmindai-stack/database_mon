"""Bloklama geçmişi canlı testleri arasında PAYLAŞILAN düzenek (Faz 31 Commit 10a / 10c takip 3).

PostgreSQL (`tests/test_sampling_cadence_live.py`) ve SQL Server (`tests/test_blocking_live_mssql.py`)
aynı senaryoyu (gerçek kilit çatışması → örnekleme → yazım → olay) iki motorda kanıtlıyor. Bunlar AYRI
dosyalarda olmalı çünkü PostgreSQL testi yalnızca `LIVE_DSNS`'e, SQL Server testi yalnızca `MSSQL_TARGETS`'a
bağlı olmalı — tek bir dosyada, tek bir modül-seviyeli `pytestmark` ikisini de AYNI kaynağa zincirlerdi.
Bu tam olarak Commit 10c takip 3'te bulunan hata: SQL Server testi, `test_sampling_cadence_live.py`'nin
`skipif(not LIVE_DSNS, ...)` modül işaretine miras kaldığı için PostgreSQL DSN'i tanımlı DEĞİLKEN de
(CI'nin `live-mssql` işinde) atlanıyordu; PostgreSQL DSN'i tanımlıyken (CI'nin `live-postgres` işinde) ama
SQL Server hedefi YOKKEN de kendi (doğru gerekçeli ama "sürüm koşulu" örüntüsüyle eşleşmeyen) atlaması
`tests/conftest.py`nin canlı-test-atlama denetimini haksız yere kırmızı yapıyordu — SQL Server'ın olup
olmaması PostgreSQL işinin sorumluluğunda değil.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import BlockingEpisode, Instance
from app.services import blocking_history, wait_sampling
from app.services.credentials import encrypt_secret


def log(title: str, value: Any) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(autouse=True)
async def clean_sampling_state():
    await init_db()
    wait_sampling.reset_state()
    blocking_history.reset_state()
    yield
    # Bellek instance'larının (meta veritabanında satırı yok) kovaları yazılmaz: yabancı anahtar hatası verirdi.
    # Veritabanı destekli testler kendi kovalarını açıkça yazıyor.
    wait_sampling._buckets.clear()
    wait_sampling._pending_flush.clear()
    await wait_sampling.shutdown_sampling()
    wait_sampling.reset_state()
    blocking_history.reset_state()


@contextlib.contextmanager
def pin_instances(instances: list[Instance]):
    """Tur, meta veritabanından okumak yerine bu instance'ları kullansın; çıkışta özgün işlevi geri koyar."""
    original = wait_sampling._instances_for_tick

    async def fixed(now, *, refresh):
        return list(instances)

    wait_sampling._instances_for_tick = fixed
    try:
        yield
    finally:
        wait_sampling._instances_for_tick = original


async def watch_blocking(instance: Instance, collector_calls: list[float], hold_seconds: float, blocker) -> float:
    """Kilit çatışması sırasında ve öncesinde gerçek örnekleme + yazım işini koşturur."""
    real = wait_sampling.BLOCKING_CHECK_INTERVAL_SECONDS
    wait_sampling.BLOCKING_CHECK_INTERVAL_SECONDS = 2.0  # testi kısaltmak için (üretimde 10 sn)
    try:
        with pin_instances([instance]):
            sampler = await wait_sampling._ensure_sampler(instance)
            original = sampler.collector.collect_blocking

            async def counting(*args, **kwargs):
                collector_calls.append(time.monotonic())
                return await original(*args, **kwargs)

            sampler.collector.collect_blocking = counting  # type: ignore[method-assign]

            started = time.monotonic()
            release_at = None
            for tick in range(int(hold_seconds) + 16):
                if tick == 4:
                    blocker["start"]()
                    release_at = time.monotonic() + hold_seconds
                if release_at is not None and time.monotonic() >= release_at and not blocker.get("released"):
                    blocker["release"]()
                    blocker["released"] = True
                await wait_sampling.sampling_tick(wait=False, flush=False)
                await wait_sampling.flush_tick(refresh_instances=False)
                await asyncio.sleep(max(0.0, started + (tick + 1) - time.monotonic()))
            return started
    finally:
        wait_sampling.BLOCKING_CHECK_INTERVAL_SECONDS = real


async def episodes(instance_id: int) -> list[BlockingEpisode]:
    async with SessionLocal() as session:
        return list((await session.execute(select(BlockingEpisode).where(
            BlockingEpisode.instance_id == instance_id))).scalars().all())


async def register_row(engine: str, target) -> Instance:
    async with SessionLocal() as session:
        row = Instance(name=f"blk-{uuid.uuid4().hex[:6]}", engine=engine, host=target.host, port=target.port,
                       database=target.database, username=target.username, password=encrypt_secret(target.password),
                       options=target.options or None, enabled=True)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


def assert_blocking_episode_was_recorded(before_started: float, calls: list[float], episode_rows: list[BlockingEpisode]) -> None:
    """Her iki motorda da AYNI iddia: sessizken sorgu yok, çatışırken var, olay kaydedilip kapanıyor."""
    before_block = [c for c in calls if c - before_started < 4]
    log("bloklama sorgusu sayısı (öncesi / toplam)", (len(before_block), len(calls)))
    assert before_block == [], "kilit bekleyen oturum yokken hedefe bloklama sorgusu gitmemeli"
    assert calls, "gerçek kilit çatışması sırasında ağaç okunmalı"
    log("olay", [(e.root_pid, e.max_blocked_sessions, round(e.duration_seconds or 0, 1), e.ended_at is not None)
                 for e in episode_rows])
    assert episode_rows and episode_rows[0].max_blocked_sessions >= 1
    assert episode_rows[0].ended_at is not None, "serbest bırakılınca olay kapanmalı (açık olay varken kontrol sürdü)"
