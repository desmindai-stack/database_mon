"""İzleme eşiğinin saydığı çağrı sayısı (Faz 31 Commit 4) — gerçek SQLite veritabanı.

İki hata burada sabitleniyor:
1. `collected_at` toplama döngüsünde SUNUCU varsayılanından geliyor; SQLite onu mikrosaniyesiz
   saklıyor ve eşitlik sorgusu eşleşmiyordu → çağrı sayısı hep None, izleme hiç "hazır" olmuyordu.
2. dbace'in kendi rolünden gelen satırlar (ör. gerçek değerli EXPLAIN ANALYZE'ın iç içe çağrısı)
   uygulama çağrısı sayılmamalı; üst düzey satır iç içe satırdan önce gelir.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample
from app.services.credentials import encrypt_secret
from app.services.index_advice_watch import current_calls


async def _instance(session) -> Instance:
    instance = Instance(
        name=f"calls-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"),
    )
    session.add(instance)
    await session.flush()
    return instance


def _row(instance_id, queryid, calls, *, from_monitoring_role=False, toplevel=True, collected_at=None):
    return SlowQuerySample(
        instance_id=instance_id, queryid=queryid, query="SELECT 1", calls=calls, total_time_ms=1.0,
        mean_time_ms=1.0, rows=1, from_monitoring_role=from_monitoring_role, toplevel=toplevel,
        **({"collected_at": collected_at} if collected_at else {}),
    )


async def test_rows_written_with_the_server_default_timestamp_are_counted():
    await init_db()
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(_row(instance.id, "Q-default", 7))  # collected_at: sunucu varsayılanı
        await session.commit()
        assert await current_calls(session, instance.id, query="SELECT 1", queryid="Q-default") == 7


async def test_monitoring_role_rows_are_excluded_and_top_level_wins_over_nested():
    await init_db()
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add_all([
            _row(instance.id, "Q-mix", 2, toplevel=True, collected_at=now),
            _row(instance.id, "Q-mix", 40, toplevel=False, collected_at=now),
            _row(instance.id, "Q-mix", 900, from_monitoring_role=True, collected_at=now),
        ])
        await session.commit()
        assert await current_calls(session, instance.id, query="SELECT 1", queryid="Q-mix") == 2


async def test_only_nested_rows_are_used_when_there_is_no_top_level_row():
    await init_db()
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(_row(instance.id, "Q-nested", 11, toplevel=False))
        await session.commit()
        assert await current_calls(session, instance.id, query="SELECT 1", queryid="Q-nested") == 11


async def test_an_older_top_level_cycle_wins_over_a_newer_cycle_that_only_has_the_nested_row():
    """Canlıda (PG 16) ölçülen durum: son döngüde üst düzey satır ilk 20'ye girmedi, iç içe girdi."""
    from datetime import timedelta

    await init_db()
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add_all([
            _row(instance.id, "Q-cycles", 2, toplevel=True, collected_at=now - timedelta(minutes=5)),
            _row(instance.id, "Q-cycles", 4, toplevel=False, collected_at=now),
        ])
        await session.commit()
        assert await current_calls(session, instance.id, query="SELECT 1", queryid="Q-cycles") == 2
