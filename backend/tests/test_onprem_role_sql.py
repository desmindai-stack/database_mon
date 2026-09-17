"""On-prem paketinin izleme rolü SQL'i yalnızca OKUMA yetkisi veriyor; dbace'in kodunda yazma yolu yok
(Faz 31 Commit 8).

Bankadaki kısıt: süper kullanıcı, pg_read/write_server_files, CREATE/TEMP, CREATE EXTENSION yok; SQL Server'da
sysadmin/db_owner yok. Denetim İZİN LİSTESİYLE: DBA'nın çalıştıracağı her ifade bilinen, salt-okunur kalıplardan
birine uymak ZORUNDA (yasak listesi yeni bir yazma yetkisini kaçırabilirdi) ve her GRANT/CREATE satırı hangi
özellik için gerektiğini yanındaki yorumda söylüyor. Negatif kontroller aynı denetleyiciye bilerek hatalı SQL veriyor.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PG_SQL = ROOT / "deploy" / "onprem" / "sql" / "postgresql-monitor-role.sql"
MS_SQL = ROOT / "deploy" / "onprem" / "sql" / "sqlserver-monitor-login.sql"

_IDENT = r"(?:\w+|<[^>]+>)"
PG_ALLOWED = (
    re.compile(rf"^CREATE ROLE {_IDENT} LOGIN PASSWORD (?::'\w+'|'[^']*');$", re.I),
    re.compile(rf"^GRANT pg_monitor TO {_IDENT};$", re.I),
    re.compile(rf"^GRANT CONNECT ON DATABASE {_IDENT} TO {_IDENT};$", re.I),
    re.compile(rf"^GRANT USAGE ON SCHEMA {_IDENT} TO {_IDENT};$", re.I),
    re.compile(rf"^GRANT SELECT ON ALL TABLES IN SCHEMA {_IDENT} TO {_IDENT};$", re.I),
)
_MS_NAME = r"\[[^\]]+\]"
MS_ALLOWED = (
    re.compile(rf"^CREATE LOGIN {_MS_NAME} WITH PASSWORD = N'[^']*', CHECK_POLICY = ON;$", re.I),
    re.compile(rf"^GRANT VIEW SERVER STATE TO {_MS_NAME};$", re.I),
    re.compile(rf"^USE {_MS_NAME};$", re.I),
    re.compile(rf"^CREATE USER {_MS_NAME} FOR LOGIN {_MS_NAME};$", re.I),
    re.compile(rf"^GRANT VIEW DATABASE STATE TO {_MS_NAME};$", re.I),
)
#: İzin listesine ek, bağımsız ikinci kilit: bu kelimeler yorum DIŞINDA hiç geçmemeli.
FORBIDDEN = re.compile(
    r"\b(SUPERUSER|CREATEDB|CREATEROLE|REPLICATION|BYPASSRLS|TEMP|TEMPORARY|INSERT|UPDATE|DELETE|TRUNCATE|"
    r"EXTENSION|pg_read_server_files|pg_write_server_files|pg_execute_server_program|ALL PRIVILEGES|"
    r"sysadmin|db_owner|db_datawriter|db_ddladmin|CONTROL|ALTER|IMPERSONATE|ADD MEMBER|sp_addsrvrolemember|"
    r"sp_addrolemember|GRANT OPTION|ADMIN OPTION)\b",
    re.I,
)


def violations(sql: str, allowed: tuple[re.Pattern[str], ...]) -> list[str]:
    problems: list[str] = []
    for number, line in enumerate(sql.splitlines(), 1):
        code, _, comment = line.partition("--")
        code = code.strip()
        if not code:
            continue
        if FORBIDDEN.search(code):
            problems.append(f"{number}: yasak yetki: {code}")
        if not any(p.match(code) for p in allowed):
            problems.append(f"{number}: izin listesinde olmayan ifade: {code}")
        if code.upper().startswith(("GRANT", "CREATE")) and not comment.strip():
            problems.append(f"{number}: hangi özellik için gerektiği yazmıyor: {code}")
    return problems


@pytest.mark.parametrize(("path", "allowed"), [(PG_SQL, PG_ALLOWED), (MS_SQL, MS_ALLOWED)], ids=["postgresql", "sqlserver"])
def test_package_role_sql_grants_only_read_and_explains_every_grant(path, allowed):
    sql = path.read_text(encoding="utf-8")
    statements = [line.partition("--")[0].strip() for line in sql.splitlines() if line.partition("--")[0].strip()]
    assert len(statements) >= 5, "rol SQL'i boş ya da tamamen yorum olmamalı"
    assert violations(sql, allowed) == []


@pytest.mark.parametrize(("bad", "allowed"), [
    ("CREATE ROLE dbace_monitor LOGIN SUPERUSER PASSWORD 'x';  -- bağlantı", PG_ALLOWED),
    ("GRANT pg_read_server_files TO dbace_monitor;  -- log", PG_ALLOWED),
    ("GRANT pg_write_server_files TO dbace_monitor;  -- log", PG_ALLOWED),
    ("GRANT TEMPORARY ON DATABASE app TO dbace_monitor;  -- ifade index'i", PG_ALLOWED),
    ("GRANT CREATE ON SCHEMA public TO dbace_monitor;  -- index", PG_ALLOWED),
    ("GRANT INSERT ON ALL TABLES IN SCHEMA public TO dbace_monitor;  -- yazma", PG_ALLOWED),
    ("GRANT ALL PRIVILEGES ON DATABASE app TO dbace_monitor;  -- her şey", PG_ALLOWED),
    ("CREATE EXTENSION hypopg;  -- fayda ölçümü", PG_ALLOWED),
    ("ALTER ROLE dbace_monitor CREATEDB;  -- ?", PG_ALLOWED),
    ("GRANT pg_monitor TO dbace_monitor;", PG_ALLOWED),  # yorum yok
    ("GRANT pg_signal_backend TO dbace_monitor;  -- oturum sonlandırma", PG_ALLOWED),  # izin listesi dışı
    ("ALTER SERVER ROLE sysadmin ADD MEMBER [dbace_monitor];  -- her şey", MS_ALLOWED),
    ("ALTER ROLE db_owner ADD MEMBER [dbace_monitor];  -- index", MS_ALLOWED),
    ("GRANT CONTROL SERVER TO [dbace_monitor];  -- her şey", MS_ALLOWED),
    ("EXEC sp_addrolemember 'db_datawriter', 'dbace_monitor';  -- yazma", MS_ALLOWED),
    ("GRANT VIEW SERVER STATE TO [dbace_monitor];", MS_ALLOWED),  # yorum yok
    ("GRANT VIEW ANY DEFINITION TO [dbace_monitor];  -- şema", MS_ALLOWED),  # izin listesi dışı
])
def test_negative_control_checker_catches_write_or_undocumented_grants(bad, allowed):
    assert violations(bad, allowed), f"denetleyici kaçırdı: {bad}"


# --- Kodda yazma/TEMP yolu yok --------------------------------------------------------------------


def _inventory_module():
    sys.path.insert(0, str(ROOT / "backend" / "scripts"))
    import permission_inventory

    return permission_inventory


def test_code_runs_no_ddl_dml_or_server_file_function_on_target_databases():
    inventory = _inventory_module().build_inventory()
    objects = {obj for _, obj in inventory.objects}
    assert len(objects) >= 50, "envanter taraması boş döndü — AST çıkarımı kırılmış olabilir"
    assert inventory.writes == [], f"hedefe yazan ifade: {inventory.writes}"
    assert not objects & {"pg_read_file", "pg_read_binary_file", "pg_ls_logdir", "pg_ls_dir", "pg_stat_file"}, objects


def test_negative_control_inventory_sees_temp_table_and_server_file_reads(tmp_path):
    module = _inventory_module()
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "bad.py").write_text(
        "LOG_SQL = \"SELECT pg_read_file(pg_current_logfile())\"\n\n"
        "async def check(conn, expr):\n"
        "    await conn.execute(f\"CREATE TEMP TABLE dbace_probe (LIKE t)\")\n"
        "    await conn.execute(\"CREATE INDEX ON dbace_probe ((\" + expr + \"))\")\n"
        "    return await conn.fetchval(LOG_SQL)\n",
        encoding="utf-8",
    )
    inventory = module.build_inventory(tmp_path)
    assert inventory.writes == ["services/bad:4", "services/bad:5"]
    assert ("postgresql", "pg_read_file") in inventory.objects
