"""Canlı SQL Server testlerinin ortak kurulumu (Faz 31 Commit 6). Konteynerler: scripts/live_mssql.py.

Hedefler tanımlıyken canlı SQL Server testi sürüm koşulu dışında atlanırsa oturum kırmızı
(tests/conftest.py atlama denetimi — PostgreSQL ile aynı kural).
"""

from __future__ import annotations

import os
from pathlib import Path

from app.collectors.base import ConnectionTarget

MSSQL_TARGETS = {
    kind: value.strip()
    for kind, value in (
        ("standalone", os.environ.get("DBACE_TEST_MSSQL_STANDALONE", "")),
        ("ag", os.environ.get("DBACE_TEST_MSSQL_AG", "")),
    )
    if value.strip()
}
MSSQL_SKIP_REASON = (
    "Gerçek SQL Server yok. `python scripts/live_mssql.py up` ile konteynerleri kurup yazdırdığı "
    "DBACE_TEST_MSSQL_* değerlerini tanımlayın."
)
LOGINS = {"ro": ("dbace_ro", "Dbace!Ro_pw1"), "noperm": ("dbace_noperm", "Dbace!No_pw1")}


def odbc_options() -> dict:
    driver = os.environ.get("DBACE_TEST_MSSQL_ODBC_DRIVER", "").strip()
    if not driver or driver == "ODBC Driver 18 for SQL Server":
        return {}
    # Windows'un eski "SQL Server" sürücüsü TLS 1.2 şifrelemeyi desteklemiyor (ölçüldü).
    return {"odbc_driver": driver, "encrypt": False}


def mssql_target(kind: str, login: str) -> ConnectionTarget:
    host, port = MSSQL_TARGETS[kind].split(",")
    user, password = LOGINS[login]
    return ConnectionTarget(host=host, port=int(port), database="master", username=user, password=password,
                            options=odbc_options())


# --- Paketin izleme login'i (Faz 31 Commit 8) ----------------------------------------------------

SA_PASSWORD = "Dbace!Passw0rd"
#: deploy/onprem/sql/sqlserver-monitor-login.sql ile kurulan login ve izlenen uygulama veritabanı.
MONITOR_LOGIN, MONITOR_PASSWORD = "dbace_monitor", "Dbace!Mon_pw1"
APP_DATABASE = "dbace_it_app"
#: İzleme login'lerinin KULLANICISI OLMAYAN veritabanı — "veritabanı açılamıyor" durumunu ölçmek için.
NO_USER_DATABASE = "dbace_it_nouser"
PACKAGE_LOGIN_SQL = Path(__file__).resolve().parents[2] / "deploy" / "onprem" / "sql" / "sqlserver-monitor-login.sql"


def standalone_target(user: str, password: str, database: str) -> ConnectionTarget:
    host, port = MSSQL_TARGETS["standalone"].split(",")
    return ConnectionTarget(host=host, port=int(port), database=database, username=user, password=password,
                            options=odbc_options())


def render_login_sql(sql: str, *, database: str, password: str) -> str:
    """DBA'nın yapacağı yer değiştirmeler: <güçlü_parola>, <izlenen_veritabanı>."""
    return sql.replace("<güçlü_parola>", password.replace("'", "''")).replace("<izlenen_veritabanı>", database)


def prepare_monitor_login() -> None:
    """Uygulama veritabanı + paket login SQL'i SIFIRDAN — idempotent, senkron (pyodbc)."""
    import pyodbc

    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    conn = pyodbc.connect(build_odbc_connection_string(standalone_target("sa", SA_PASSWORD, "master")), autocommit=True)
    try:
        cur = conn.cursor()
        for database in (APP_DATABASE, NO_USER_DATABASE):
            cur.execute(f"IF DB_ID('{database}') IS NULL CREATE DATABASE {database}")
        for database in ("master", "msdb", APP_DATABASE):
            cur.execute(f"USE [{database}]; IF USER_ID('{MONITOR_LOGIN}') IS NOT NULL DROP USER [{MONITOR_LOGIN}]")
        cur.execute("USE [master]")
        for (spid,) in cur.execute("SELECT session_id FROM sys.dm_exec_sessions WHERE login_name = ?", MONITOR_LOGIN).fetchall():
            cur.execute(f"KILL {int(spid)}")
        cur.execute(f"IF SUSER_ID('{MONITOR_LOGIN}') IS NOT NULL DROP LOGIN [{MONITOR_LOGIN}]")
        cur.execute(render_login_sql(PACKAGE_LOGIN_SQL.read_text(encoding="utf-8"), database=APP_DATABASE,
                                     password=MONITOR_PASSWORD))
        cur.execute(f"USE [{APP_DATABASE}]; IF OBJECT_ID('dbo.deadlock_probe') IS NULL "
                    "CREATE TABLE dbo.deadlock_probe (id int PRIMARY KEY, v int NOT NULL); "
                    "IF NOT EXISTS (SELECT 1 FROM dbo.deadlock_probe) INSERT dbo.deadlock_probe VALUES (1, 0), (2, 0);")
        # Yetki matrisi karşılaştırması için: VERİTABANI YETKİSİZ kullanıcılar (dbace_ro'nun yalnızca VIEW SERVER
        # STATE'i, dbace_noperm'in hiçbir şeyi var). Böylece üç login aynı veritabanında ölçülüyor.
        for login in (LOGINS["ro"][0], LOGINS["noperm"][0]):
            cur.execute(f"USE [{APP_DATABASE}]; IF USER_ID('{login}') IS NULL CREATE USER [{login}] FOR LOGIN [{login}]")
    finally:
        conn.close()
