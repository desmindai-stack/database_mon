"""Yetki envanteri: dbace'in hedef veritabanlarında GERÇEKTEN çalıştırdığı SQL'in dokunduğu sistem nesneleri
(Faz 31 Commit 8).

Koddan çıkarılıyor, elle değil: `app/` altındaki bütün hedef bağlantı çağrıları (`execute`, `fetch*`) AST ile
bulunuyor; argüman sabitse, f-string'se ya da bir ada/`.format` çağrısına bağlıysa tanımına kadar çözülüyor.
Öneri METİNLERİNDE geçen nesneler (DBA'nın çalıştıracağı komutlar) sayılmıyor — ilk sürüm bunları da sayıp
`msdb.dbo.sysjobs` için gereksiz yetki istiyordu.

    python scripts/permission_inventory.py                  # envanteri yazdır
    python scripts/permission_inventory.py --measure --write # kısıtlı rollerle ölç, matrisi pakete yaz

`--measure` (canlı konteynerler: scripts/live_pg.py, scripts/live_mssql.py): her nesne ÜÇ rolle okunuyor ve
paketin rolüyle AYNI sonucu veren en az yetkili rol "gereken yetki" olarak yazılıyor — gereken yetki de elle değil:
- PostgreSQL 15/16/17, `dbace_restricted` (TEMP/CREATE PUBLIC'ten alınmış): `dbace_it_bare` (hiçbir yetki),
  `dbace_it_monitor` (yalnızca pg_monitor), `dbace_monitor` (PAKETİN rol SQL'iyle kurulan: pg_monitor + SELECT).
- SQL Server, `dbace_it_app`: `dbace_noperm` (veritabanında kullanıcı, yetki yok), `dbace_ro` (VIEW SERVER STATE),
  `dbace_monitor` (PAKETİN login SQL'iyle: VIEW SERVER STATE + VIEW DATABASE STATE).
Karşılaştırılan sonuç: satır sayısı + yetki yüzünden maskelenen/NULL gelen satır sayısı; hata = ❌.
`--write`: `deploy/onprem/sql/permission-matrix.md`. Paketteki matris ile koddaki envanter ayrışırsa
`tests/test_onprem_package_drift.py` kırmızı.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
MATRIX_PATH = BACKEND.parent / "deploy" / "onprem" / "sql" / "permission-matrix.md"

SQL_METHODS = {"execute", "fetch", "fetchval", "fetchrow", "fetchmany", "fetchall", "executemany"}
PG_OBJECT = re.compile(
    r"\b(pg_stat_[a-z_]+|pg_statio_[a-z_]+|pg_locks|pg_settings|pg_stats|pg_prepared_statements|pg_replication_slots|"
    r"pg_database|pg_class|pg_index|pg_indexes|pg_namespace|pg_attribute|pg_extension|pg_roles|pg_proc|pg_type|"
    r"pg_constraint|pg_inherits|pg_tablespace|pg_ls_[a-z_]+|pg_read_(?:binary_)?file|pg_current_logfile|pg_current_wal_lsn|"
    r"pg_last_wal_receive_lsn|pg_last_wal_replay_lsn|pg_wal_lsn_diff|pg_database_size|pg_total_relation_size|"
    r"pg_relation_size|pg_table_size|pg_indexes_size|pg_blocking_pids|pg_is_in_recovery|pg_get_indexdef|"
    r"pg_postmaster_start_time|hypopg_[a-z_]+|pg_buffercache|pgstattuple[a-z_]*)\b"
)
MS_OBJECT = re.compile(r"\b(sys\.[a-z_]+|msdb\.dbo\.[a-z_]+)\b", re.IGNORECASE)
#: Metin ifadeler (DDL/DML) — çalıştırılıyorsa yazma yetkisi ister; envanterde ayrıca raporlanır.
WRITE_STATEMENT = re.compile(r"^\s*(?:/\*.*?\*/\s*)?(CREATE|ALTER|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE|DROP|COPY)\b", re.I | re.S)


@dataclass
class Inventory:
    objects: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)


_MODULE_CACHE: dict[str, ast.Module] = {}


def _module(dotted: str) -> ast.Module | None:
    path = BACKEND / (dotted.replace(".", "/") + ".py")
    if not path.exists():
        return None
    if dotted not in _MODULE_CACHE:
        _MODULE_CACHE[dotted] = ast.parse(path.read_text(encoding="utf-8"))
    return _MODULE_CACHE[dotted]


def _imported(name: str, scopes: list[ast.AST]) -> ast.Module | None:
    """`from app.services.deadlocks import SQLSERVER_DEADLOCK_SQL` → o modül."""
    for scope in scopes:
        for child in ast.walk(scope):
            if isinstance(child, ast.ImportFrom) and child.module and any(a.name == name for a in child.names):
                return _module(child.module)
    return None


def _string_of(node: ast.AST, scopes: list[ast.AST]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else " {} " for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, right = _string_of(node.left, scopes), _string_of(node.right, scopes)
        return (left or "") + (right or "") if left or right else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "replace", "strip"):
        return _string_of(node.func.value, scopes)
    if isinstance(node, ast.Name):
        for scope in scopes:
            for child in ast.walk(scope):
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    if any(isinstance(t, ast.Name) and t.id == node.id for t in targets) and child.value is not None:
                        text = _string_of(child.value, scopes)
                        if text:
                            return text
        source = _imported(node.id, scopes)
        if source is not None:
            return _string_of(node, [source])
    return None


def build_inventory(app_dir: Path = BACKEND / "app") -> Inventory:
    inventory = Inventory()
    for path in sorted(app_dir.rglob("*.py")):
        if path.name in ("database.py", "migrations_runner.py"):
            continue  # dbace'in KENDİ veritabanı
        module = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(app_dir).with_suffix("").as_posix()
        functions = [n for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(module):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in SQL_METHODS and node.args):
                continue
            receiver = ast.unparse(node.func.value)
            if "session" in receiver or receiver in ("db", "s"):
                continue  # SQLAlchemy — dbace'in kendi veritabanı
            enclosing = [f for f in functions if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)]
            text = _string_of(node.args[0], enclosing[-1:] + [module])
            if not text:
                continue
            if WRITE_STATEMENT.search(text):
                inventory.writes.append(f"{rel}:{node.lineno}")
            for match in MS_OBJECT.finditer(text):
                inventory.objects.setdefault(("sqlserver", match.group(1).lower()), set()).add(rel)
            if not MS_OBJECT.search(text):
                for match in PG_OBJECT.finditer(text):
                    inventory.objects.setdefault(("postgresql", match.group(1)), set()).add(rel)
    return inventory


# --- Ölçüm --------------------------------------------------------------------------------------

PG_PORTS = {15: 55433, 16: 55434, 17: 55432}
PG_FUNCTION_PROBES = {
    "pg_current_wal_lsn": "SELECT pg_current_wal_lsn()",
    "pg_last_wal_receive_lsn": "SELECT pg_last_wal_receive_lsn()",
    "pg_last_wal_replay_lsn": "SELECT pg_last_wal_replay_lsn()",
    "pg_wal_lsn_diff": "SELECT pg_wal_lsn_diff('0/0', '0/0')",
    "pg_database_size": "SELECT pg_database_size(current_database())",
    "pg_total_relation_size": "SELECT pg_total_relation_size('orders')",
    "pg_relation_size": "SELECT pg_relation_size('orders')",
    "pg_table_size": "SELECT pg_table_size('orders')",
    "pg_indexes_size": "SELECT pg_indexes_size('orders')",
    "pg_blocking_pids": "SELECT pg_blocking_pids(pg_backend_pid())",
    "pg_is_in_recovery": "SELECT pg_is_in_recovery()",
    "pg_get_indexdef": "SELECT pg_get_indexdef(indexrelid) FROM pg_index LIMIT 1",
    "pg_postmaster_start_time": "SELECT pg_postmaster_start_time()",
    "hypopg_create_index": "SELECT count(*) FROM hypopg_create_index('CREATE INDEX ON orders (status)')",
    "hypopg_get_indexdef": "SELECT hypopg_get_indexdef(indexrelid) FROM hypopg_create_index('CREATE INDEX ON orders (total)')",
    "hypopg_drop_index": "SELECT hypopg_reset()",
    "hypopg_reset": "SELECT hypopg_reset()",
}
MS_FUNCTION_PROBES = {
    "sys.dm_exec_sql_text": "SELECT TOP 1 1 AS one FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t",
    "sys.dm_exec_query_plan": "SELECT TOP 1 1 AS one FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) p",
    "sys.dm_io_virtual_file_stats": "SELECT TOP 1 1 AS one FROM sys.dm_io_virtual_file_stats(NULL, NULL)",
    "sys.fn_xe_file_target_read_file": "SELECT TOP 1 1 AS one FROM sys.fn_xe_file_target_read_file('system_health*.xel', NULL, NULL, NULL)",
    "sys.dm_db_index_physical_stats": "SELECT TOP 1 1 AS one FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED')",
}

#: (anahtar, rol, gereken yetki etiketi) — EN AZ yetkiliden paketin rolüne.
PG_ROLES = (("bare", "dbace_it_bare", "ek yetki yok (PUBLIC)"),
            ("select", "dbace_it_app", "tabloda SELECT"),
            ("monitor", "dbace_it_monitor", "pg_monitor"),
            ("package", "dbace_monitor", "pg_monitor + tabloda SELECT"))
MS_ROLES = (("bare", "dbace_noperm", "Dbace!No_pw1", "ek yetki yok (public)"),
            ("vss", "dbace_ro", "Dbace!Ro_pw1", "VIEW SERVER STATE"),
            ("package", "dbace_monitor", "Dbace!Mon_pw1", "VIEW DATABASE STATE (izlenen veritabanında)"))
_MISSING = "sürümde yok"
_PG_ORDER = [label for _, _, label in PG_ROLES]
#: Ölçüm rollerinin kendi pg_stat_statements satırları ölçüm SIRASINDA artıyor — karşılaştırmadan çıkarılıyor.
_OWN_ROLES = ", ".join(f"'{role}'" for _, role, _ in PG_ROLES)


def _pg_probe_sql(obj: str) -> str:
    if obj in PG_FUNCTION_PROBES:
        return f"SELECT count(*)::text FROM ({PG_FUNCTION_PROBES[obj]}) AS probe"
    source = obj
    if obj == "pg_stat_statements":
        source = f"pg_stat_statements WHERE userid NOT IN (SELECT oid FROM pg_roles WHERE rolname IN ({_OWN_ROLES}))"
    # Satır sayısı / "<insufficient privilege>" satır sayısı / NULL alan toplamı. pg_monitor'süz rol hata ALMIYOR:
    # başka oturumların sorgu metnini maskeli, replikasyon kolonlarını NULL görüyor; ayarların bir kısmını hiç görmüyor.
    return (
        "SELECT count(*) || '/' || count(*) FILTER (WHERE to_jsonb(x)::text LIKE '%insufficient privilege%') || '/' || "
        "coalesce(sum((SELECT count(*) FROM jsonb_each(to_jsonb(x)) e WHERE e.value = 'null'::jsonb)), 0) "
        f"FROM (SELECT * FROM {source}) x"
    )


def _coarse(value: str) -> str:
    """Oynak nesnelerde (satır sayısı ölçüm sırasında değişiyor) kaba karşılaştırma: hata / boş / var / maskeli."""
    if value.startswith("❌") or value == _MISSING:
        return value
    parts = value.split("/")
    rows = int(parts[0])
    masked = len(parts) > 1 and int(parts[1]) > 0
    return "boş" if rows == 0 else ("maskeli" if masked else "var")


def _needed(seen: dict[str, str], stable: bool, roles) -> str:
    package = seen["package"]
    key_of = (lambda v: v) if stable else _coarse
    return next(role[-1] for role in roles if key_of(seen[role[0]]) == key_of(package))


async def _measure_pg(objects: list[str]) -> dict[str, dict]:
    """{nesne: {sürüm: sonuç, "needed": {gereken yetki}}}"""
    import asyncpg

    sys.path.insert(0, str(BACKEND))
    from tests.live_pg import ROLE_PASSWORD, ensure_roles, prepare_restricted_database

    async def probe(conn, obj) -> str:
        try:
            async with conn.transaction(readonly=True):
                return str(await conn.fetchval(_pg_probe_sql(obj)))
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            return _MISSING if "Undefined" in name else f"❌ {name}"

    result: dict[str, dict] = {obj: {"needed": set()} for obj in objects}
    for version, port in PG_PORTS.items():
        dsn = f"postgresql://postgres:dbace@127.0.0.1:{port}/dbace"
        server = await asyncpg.connect(dsn)
        try:
            await ensure_roles(server)
        finally:
            await server.close()
        await prepare_restricted_database(dsn)
        conns = {key: await asyncpg.connect(host="127.0.0.1", port=port, user=user, password=ROLE_PASSWORD,
                                            database="dbace_restricted", statement_cache_size=0)
                 for key, user, _ in PG_ROLES}
        try:
            for obj in objects:
                before = await probe(conns["package"], obj)
                seen = {key: await probe(conns[key], obj) for key, _, _ in PG_ROLES}
                if seen["package"] == _MISSING or seen["package"].startswith("❌"):
                    result[obj][version] = seen["package"]
                    continue
                result[obj][version] = "✅"
                result[obj]["needed"].add(_needed(seen, before == seen["package"], PG_ROLES))
        finally:
            for conn in conns.values():
                await conn.close()
    return result


def _measure_ms(objects: list[str], driver: str) -> dict[str, dict]:
    import os

    import pyodbc

    os.environ.setdefault("DBACE_TEST_MSSQL_STANDALONE", "127.0.0.1,14333")
    os.environ.setdefault("DBACE_TEST_MSSQL_ODBC_DRIVER", driver)
    sys.path.insert(0, str(BACKEND))
    from tests.live_mssql import APP_DATABASE, prepare_monitor_login

    prepare_monitor_login()
    encrypt = "no" if driver == "SQL Server" else "yes;TrustServerCertificate=yes"
    conns = {key: pyodbc.connect(f"DRIVER={{{driver}}};SERVER=127.0.0.1,14333;DATABASE={APP_DATABASE};UID={user};"
                                 f"PWD={password};Encrypt={encrypt}", timeout=5, autocommit=True)
             for key, user, password, _ in MS_ROLES}

    def probe(conn, obj) -> str:
        sql = MS_FUNCTION_PROBES.get(obj) or f"SELECT 1 AS one FROM {obj}"
        try:
            return str(conn.cursor().execute(f"SELECT COUNT(*) FROM ({sql}) AS probe").fetchval())
        except Exception as exc:  # noqa: BLE001
            return "❌ " + ("yetki" if "permission" in str(exc).lower() else str(exc)[:60])

    result: dict[str, dict] = {}
    try:
        for obj in objects:
            before = probe(conns["package"], obj)
            seen = {key: probe(conns[key], obj) for key, _, _, _ in MS_ROLES}
            if seen["package"].startswith("❌"):
                result[obj] = {"measured": seen["package"], "needed": "—"}
                continue
            result[obj] = {"measured": "✅", "needed": _needed(seen, before == seen["package"], MS_ROLES)}
    finally:
        for conn in conns.values():
            conn.close()
    return result


def render(inventory: Inventory, pg: dict | None, ms: dict | None) -> str:
    lines = [
        "# dbace yetki matrisi (ÜRETİLEN — elle düzenlemeyin)",
        "",
        "`python backend/scripts/permission_inventory.py --measure --write` ile üretilir. Nesneler dbace'in hedef",
        "veritabanında GERÇEKTEN çalıştırdığı SQL'den (AST) çıkarılır. Ölçüm PAKETİN rol/login SQL'iyle kurulan",
        "`dbace_monitor` ile yapılır (PostgreSQL: pg_monitor + tablolarda SELECT, TEMP/CREATE YOK; SQL Server:",
        "VIEW SERVER STATE + VIEW DATABASE STATE). **Gereken yetki** de ölçümden: nesne en az yetkiliden paketin",
        "rolüne sırayla okunur (PostgreSQL: yetkisiz → yalnızca SELECT → yalnızca pg_monitor → paket; SQL Server:",
        "yetkisiz → yalnızca VIEW SERVER STATE → paket) ve paket rolüyle AYNI sonucu (satır sayısı, maskelenen satır,",
        "NULL alan; ölçüm sırasında değişen nesnelerde hata/boş/var/maskeli) veren İLK rolün yetkisi yazılır.",
        "Rol kurulumu: `postgresql-monitor-role.sql`, `sqlserver-monitor-login.sql`.",
        "",
        "| Motor | Nesne | Kullanan modüller | Gereken yetki (ölçülen) | PG 15 | PG 16 | PG 17 / SQL Server |",
        "|---|---|---|---|---|---|---|",
    ]
    for (engine, obj), modules in sorted(inventory.objects.items()):
        mods = ", ".join(sorted(modules))
        if engine == "postgresql":
            m = (pg or {}).get(obj, {})
            needed = max(m.get("needed") or [], key=_PG_ORDER.index, default="—")
            lines.append(f"| postgresql | `{obj}` | {mods} | {needed} | {m.get(15, '—')} | {m.get(16, '—')} | {m.get(17, '—')} |")
        else:
            m = (ms or {}).get(obj, {})
            lines.append(f"| sqlserver | `{obj}` | {mods} | {m.get('needed', '—')} | — | — | {m.get('measured', '—')} |")
    lines += ["", f"Hedef veritabanına yazan ifade (DDL/DML): {', '.join(inventory.writes) or 'YOK'}", ""]
    return "\n".join(lines)


def matrix_objects(markdown: str) -> set[tuple[str, str]]:
    return {(engine, obj) for engine, obj in re.findall(r"^\| (postgresql|sqlserver) \| `([^`]+)` \|", markdown, re.M)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--odbc-driver", default="ODBC Driver 18 for SQL Server")
    args = parser.parse_args()
    inventory = build_inventory()
    pg = ms = None
    if args.measure:
        pg = asyncio.run(_measure_pg(sorted(o for e, o in inventory.objects if e == "postgresql")))
        ms = _measure_ms(sorted(o for e, o in inventory.objects if e == "sqlserver"), args.odbc_driver)
    text = render(inventory, pg, ms)
    if args.write:
        MATRIX_PATH.write_text(text, encoding="utf-8", newline="\n")
        print(f"yazıldı: {MATRIX_PATH}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
