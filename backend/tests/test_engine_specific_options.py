"""Proves the engine-specific connection knobs added for wizard İŞ 1 (PostgreSQL ssl_mode,
SQL Server auth_type, MongoDB replica_set/authSource) actually affect the connection built by
the collectors — not just decorative form fields that get stored and ignored.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

from app.collectors.base import ConnectionTarget
from app.collectors.sqlserver_mongodb import (
    MongoDBCollector,
    _decode_datetimeoffset,
    build_odbc_connection_string,
    register_datetimeoffset_converter,
)


def test_windows_auth_uses_trusted_connection_and_omits_credentials():
    target = ConnectionTarget(
        host="h", port=1433, database="master", username="u", password="p", options={"auth_type": "windows"}
    )
    dsn = build_odbc_connection_string(target)
    assert "Trusted_Connection=yes" in dsn
    assert "UID=" not in dsn
    assert "PWD=" not in dsn


def test_sql_auth_is_the_default_and_uses_credentials():
    target = ConnectionTarget(host="h", port=1433, database="master", username="u", password="p", options={})
    dsn = build_odbc_connection_string(target)
    assert "UID=u;PWD=p" in dsn
    assert "Trusted_Connection" not in dsn


def test_mongo_uri_includes_replica_set_when_given():
    target = ConnectionTarget(
        host="h", port=27017, database="admin", username="u", password="p",
        options={"authSource": "admin", "replica_set": "rs0"},
    )
    collector = MongoDBCollector(target)
    uri = collector._build_uri()
    assert "authSource=admin" in uri
    assert "replicaSet=rs0" in uri


def test_mongo_uri_omits_replica_set_when_not_given():
    target = ConnectionTarget(
        host="h", port=27017, database="admin", username="u", password="p", options={"authSource": "admin"}
    )
    collector = MongoDBCollector(target)
    uri = collector._build_uri()
    assert "authSource=admin" in uri
    assert "replicaSet" not in uri


# --- `datetimeoffset` (ODBC tip -155) çözücüsü (Faz 31 Commit 10c) --------------------------------------------
#
# Gerçek SQL Server'da `sys.query_store_runtime_stats_interval.start_time`/`end_time` bu tipte; ODBC Driver 18
# (18.6.2.1, bu makinede ölçüldü) ile pyodbc bunu KENDİLİĞİNDEN çözemiyor — "ODBC SQL type -155 is not yet
# supported" ile patlıyor. `SQL_SS_TIMESTAMPOFFSET_STRUCT`'ın ham baytlarını burada ELLE üretip çözücüyü
# gerçek sürücü olmadan (offline) sınıyoruz.


def _raw_timestampoffset(year, month, day, hour, minute, second, fraction_ns, tz_hour, tz_minute) -> bytes:
    return struct.pack("<6hI2h", year, month, day, hour, minute, second, fraction_ns, tz_hour, tz_minute)


def test_decode_datetimeoffset_parses_a_utc_value_with_microseconds():
    raw = _raw_timestampoffset(2026, 9, 22, 8, 23, 44, 452_762_000, 0, 0)
    assert _decode_datetimeoffset(raw) == datetime(2026, 9, 22, 8, 23, 44, 452_762, tzinfo=timezone.utc)


def test_negative_control_a_nonzero_offset_is_not_silently_dropped():
    """UTC'ye sabitlenmiş bir çözücü bu testte YAKALANIRDI — +05:30 gibi sıfır olmayan bir dilim de
    korunmalı, yoksa 'saat dilimi taşındı ama gösterilmedi' sessiz bir veri kaybı olurdu."""
    raw = _raw_timestampoffset(2026, 9, 22, 8, 23, 44, 0, 5, 30)
    decoded = _decode_datetimeoffset(raw)
    assert decoded.utcoffset() == timedelta(hours=5, minutes=30)
    assert decoded.hour == 8  # yerel saat aynen taşınıyor, dönüştürülmüyor


async def test_register_datetimeoffset_converter_registers_type_minus_155():
    calls: list[tuple[int, object]] = []

    class FakeConn:
        async def add_output_converter(self, sqltype, func):
            calls.append((sqltype, func))

    await register_datetimeoffset_converter(FakeConn())
    assert calls == [(-155, _decode_datetimeoffset)]
