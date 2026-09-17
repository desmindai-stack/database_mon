"""Canlı SQL Server test konteynerleri — topoloji testleri için (Faz 31 Commit 6).

    python scripts/live_mssql.py up      # iki konteyner, login'ler, bozuk AG
    python scripts/live_mssql.py env     # test ortam değişkenlerini yazar

- `dbace-mssql` (14333): TEK SUNUCU, Always On kapalı.
- `dbace-mssql-ag` (14334): `MSSQL_ENABLE_HADR=1`, `CLUSTER_TYPE = NONE` bir availability group;
  ikinci replika TANIMLI ama sunucusu YOK → birincilde DISCONNECTED / NOT_HEALTHY (koparılmış replika).
- Login'ler: `dbace_ro` yalnızca VIEW SERVER STATE (+ master'da VIEW DATABASE STATE) — bankadaki izleme
  yetkisi; `dbace_noperm` hiçbir izleme yetkisi yok. sysadmin/db_owner YOK.

ODBC: dbace "ODBC Driver 18 for SQL Server" kullanıyor. Bu sürücü kurulu değilse (ör. geliştirme
makinesinde yalnızca Windows'un eski "SQL Server" sürücüsü var) `DBACE_TEST_MSSQL_ODBC_DRIVER` ile
verilir; eski sürücü TLS 1.2 şifrelemeyi desteklemediği için o durumda şifreleme kapatılır.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

SA_PASSWORD = "Dbace!Passw0rd"
RO_LOGIN, RO_PASSWORD = "dbace_ro", "Dbace!Ro_pw1"
NOPERM_LOGIN, NOPERM_PASSWORD = "dbace_noperm", "Dbace!No_pw1"
IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
CONTAINERS = {
    "standalone": ("dbace-mssql", 14333, "dbacemssql", False),
    "ag": ("dbace-mssql-ag", 14334, "dbacemssqlag", True),
}
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"

LOGINS_SQL = f"""
SET NOCOUNT ON;
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{RO_LOGIN}')
    CREATE LOGIN {RO_LOGIN} WITH PASSWORD = '{RO_PASSWORD}', CHECK_POLICY = OFF;
GRANT VIEW SERVER STATE TO {RO_LOGIN};
IF NOT EXISTS (SELECT 1 FROM master.sys.database_principals WHERE name = '{RO_LOGIN}')
    EXEC('USE master; CREATE USER {RO_LOGIN} FOR LOGIN {RO_LOGIN};');
EXEC('USE master; GRANT VIEW DATABASE STATE TO {RO_LOGIN};');
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{NOPERM_LOGIN}')
    CREATE LOGIN {NOPERM_LOGIN} WITH PASSWORD = '{NOPERM_PASSWORD}', CHECK_POLICY = OFF;
"""

AG_SQL = """
SET NOCOUNT ON;
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = 'Dbace!Mk_pw1';
IF NOT EXISTS (SELECT 1 FROM sys.certificates WHERE name = 'dbace_ag_cert')
    CREATE CERTIFICATE dbace_ag_cert WITH SUBJECT = 'dbace ag test';
IF NOT EXISTS (SELECT 1 FROM sys.database_mirroring_endpoints)
    CREATE ENDPOINT dbace_hadr STATE = STARTED AS TCP (LISTENER_PORT = 5022)
        FOR DATA_MIRRORING (ROLE = ALL, AUTHENTICATION = CERTIFICATE dbace_ag_cert, ENCRYPTION = REQUIRED ALGORITHM AES);
IF DB_ID('dbace_ag_db') IS NULL
BEGIN
    CREATE DATABASE dbace_ag_db;
    ALTER DATABASE dbace_ag_db SET RECOVERY FULL;
    BACKUP DATABASE dbace_ag_db TO DISK = '/var/opt/mssql/data/dbace_ag_db.bak' WITH INIT;
END
IF NOT EXISTS (SELECT 1 FROM sys.availability_groups WHERE name = 'dbace_ag')
    CREATE AVAILABILITY GROUP dbace_ag WITH (CLUSTER_TYPE = NONE)
    FOR DATABASE dbace_ag_db
    REPLICA ON
        N'dbacemssqlag' WITH (ENDPOINT_URL = N'tcp://dbacemssqlag:5022', AVAILABILITY_MODE = ASYNCHRONOUS_COMMIT,
                              FAILOVER_MODE = MANUAL, SEEDING_MODE = MANUAL),
        N'dbacemssqlag2' WITH (ENDPOINT_URL = N'tcp://dbacemssqlag2:5022', AVAILABILITY_MODE = ASYNCHRONOUS_COMMIT,
                               FAILOVER_MODE = MANUAL, SEEDING_MODE = MANUAL);
"""


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        sys.exit(f"KOMUT BAŞARISIZ: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def sqlcmd(container: str, sql: str, *, user: str = "sa", password: str = SA_PASSWORD, check: bool = True) -> str:
    return run("docker", "exec", container, SQLCMD, "-C", "-S", "localhost", "-U", user, "-P", password, "-b",
               "-h", "-1", "-W", "-Q", sql, check=check).stdout.strip()


def up(kind: str) -> None:
    container, port, hostname, hadr = CONTAINERS[kind]
    exists = run("docker", "ps", "-a", "--filter", f"name=^{container}$", "--format", "{{.Names}}").stdout.strip()
    if not exists:
        args = ["docker", "run", "-d", "--name", container, "-h", hostname, "-e", "ACCEPT_EULA=Y",
                "-e", f"MSSQL_SA_PASSWORD={SA_PASSWORD}", "-p", f"{port}:1433"]
        if hadr:
            args += ["-e", "MSSQL_ENABLE_HADR=1"]
        run(*args, IMAGE)
    else:
        run("docker", "start", container)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if sqlcmd(container, "SET NOCOUNT ON; SELECT 1", check=False) == "1":
            break
        time.sleep(3)
    else:
        sys.exit(f"{container} hazır olmadı")
    sqlcmd(container, LOGINS_SQL)
    if hadr:
        sqlcmd(container, AG_SQL)
    hadr_value = sqlcmd(container, "SET NOCOUNT ON; SELECT CAST(SERVERPROPERTY('IsHadrEnabled') AS varchar(3))")
    print(f"[{container}] port {port} | IsHadrEnabled={hadr_value}")


def env() -> None:
    print("DBACE_TEST_MSSQL_STANDALONE=127.0.0.1,14333")
    print("DBACE_TEST_MSSQL_AG=127.0.0.1,14334")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "env"])
    args = parser.parse_args()
    if args.command == "up":
        for kind in CONTAINERS:
            up(kind)
    env()


if __name__ == "__main__":
    main()
