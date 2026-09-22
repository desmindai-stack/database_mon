"""Bloklama geçmişi — GERÇEK SQL Server (Faz 31 Commit 10a; ayrı dosyaya taşındı, Faz 31 Commit 10c takip 3).

Paketin login SQL'iyle kurulan `dbace_monitor` (VIEW SERVER STATE, sysadmin YOK) ile ÖLÇÜLÜYOR; kilit
çatışması yönetici (`sa`) kimliğiyle üretiliyor (test düzeneği).

Bu test eskiden `tests/test_sampling_cadence_live.py`'nin İÇİNDEYDİ — o dosyanın modül-seviyeli
`pytestmark`ı PostgreSQL DSN'ine (`LIVE_DSNS`) bağlıydı, yani bu SQL Server testi de PostgreSQL DSN'i
tanımlı DEĞİLKEN atlanıyordu (SQL Server'la hiç ilgisi olmayan bir koşula miras kalmıştı). Tersi de
sorunluydu: PostgreSQL DSN'i tanımlı ama SQL Server hedefi YOKKEN (CI'nin `live-postgres` işi tam olarak
bu durumda) bu test kendi (doğru gerekçeli) `pytest.skip`'ine düşüyor, ama o gerekçe `tests/conftest.py`nin
"canlı test atlama denetimi"nin kabul ettiği TEK örüntüyle ("sürüm koşulu: sunucu X < Y") eşleşmediği için
denetim bunu YASAK bir atlama sayıp `live-postgres` işini kırmızı yapıyordu — SQL Server'ın olup olmaması
PostgreSQL işinin sorumluluğunda değil. Düzeltme: bu test artık kendi dosyasında, YALNIZCA `MSSQL_TARGETS`'a
bağlı bir `pytestmark`la — modül seviyesindeki koşulun kendisi "gerekçe" (`MSSQL_SKIP_REASON`), elle ayrı
bir metin yazılmıyor (bkz. `tests/live_mssql.py`).

Ortak düzenek (`_watch_blocking`, `_register_row`, `_episodes`, otomatik durum sıfırlama) PostgreSQL
sürümüyle PAYLAŞILIYOR: `tests/blocking_probe.py`.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests.blocking_probe import assert_blocking_episode_was_recorded, clean_sampling_state, episodes, register_row, watch_blocking
from tests.live_mssql import (
    APP_DATABASE,
    MONITOR_LOGIN,
    MONITOR_PASSWORD,
    MSSQL_SKIP_REASON,
    MSSQL_TARGETS,
    SA_PASSWORD,
    prepare_monitor_login,
    standalone_target,
)

pyodbc = pytest.importorskip("pyodbc")
pytest.importorskip("aioodbc")
pytestmark = pytest.mark.skipif("standalone" not in MSSQL_TARGETS, reason=MSSQL_SKIP_REASON)

# Yalnızca isim pytest'e görünsün diye import ediliyor (autouse fixture, tests/blocking_probe.py'de tanımlı).
_ = clean_sampling_state


async def test_sqlserver_blocking_is_read_only_when_needed_and_a_real_conflict_becomes_an_episode():
    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    await asyncio.to_thread(prepare_monitor_login)
    target = standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, APP_DATABASE)
    instance = await register_row("sqlserver", target)
    admin = build_odbc_connection_string(standalone_target("sa", SA_PASSWORD, APP_DATABASE))
    holder = await asyncio.to_thread(pyodbc.connect, admin, autocommit=False)
    waiter = await asyncio.to_thread(pyodbc.connect, admin, autocommit=True)
    state: dict = {}

    def start() -> None:
        holder.cursor().execute("UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = 1")
        state["thread"] = threading.Thread(
            target=lambda: waiter.cursor().execute("UPDATE dbo.deadlock_probe SET v = v + 1 WHERE id = 1"))
        state["thread"].start()

    def release() -> None:
        holder.commit()

    calls: list[float] = []
    try:
        started = await watch_blocking(instance, calls, 10.0, {"start": start, "release": release})
    finally:
        if "thread" in state:
            state["thread"].join(timeout=30)
        holder.close()
        waiter.close()

    assert_blocking_episode_was_recorded(started, calls, await episodes(instance.id))
