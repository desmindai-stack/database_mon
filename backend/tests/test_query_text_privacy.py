"""dbace'e yazılan sorgu metninin arındırılması (Faz 31 Commit 5).

Ölçüm ve gerekçe: `app/services/query_text_privacy.py`. Bu dosya iki şeyi sabitliyor:

1. **Kurallar** — gerçek sunucuda görülen metinlerle (pg_stat_statements, deadlock log bloğu).
2. **Yazma yolları** — sorgu metni tutan HER kolona yapılan HER yazım arındırıcıdan geçiyor.
   Kolonlar elle listelenmiyor, modellerden çıkarılıyor; yazımlar kaynak ağacından.

Gerçek toplayıcı/örnekleyici/deadlock yoluyla uçtan uca kanıt:
`test_query_text_privacy_live_postgres.py`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import Text

from app.models import Base
from app.services.query_text_privacy import (
    PASSWORD_REDACTED_NOTE,
    is_utility_statement,
    run_stored_text_cleanup,
    sanitize_deadlock_detail,
    sanitize_stored_query,
)
from app.services.sql_analysis import normalize_literals

APP = Path(__file__).resolve().parents[1] / "app"
SECRET = "gizli"

#: pg_stat_statements'ın track_utility=on iken 15.19/16.15/17.11'de SAKLADIĞI metinler (ölçüldü)
#: ve aynı sınıftan ek biçimler.
UTILITY_CORPUS = [
    "SET application_name = 'gizli_set'",
    "DO $$ BEGIN PERFORM 'gizli_do'; END $$",
    "DO $body$ BEGIN RAISE NOTICE $x$gizli$x$; END $body$",
    "SET x = $v$gizli$v$",
    "SET x = E'gizli\\'x'",
    "COPY t FROM '/tmp/gizli.csv'",
    "/* dbace */ EXPLAIN (ANALYZE) SELECT * FROM t WHERE a = 'gizli' LIMIT 20",
    "CALL transfer('gizli', 100)",
    "SET x = 'gizli",  # track_activity_query_size ile kesilmiş
]
PASSWORD_CORPUS = [
    "ALTER ROLE c5_r PASSWORD 'gizli_alter'",
    "CREATE ROLE c5_r2 PASSWORD 'gizli_create'",
    "create user u with encrypted password 'gizli'",
    "DO $body$ BEGIN EXECUTE $q$ALTER ROLE x PASSWORD 'gizli'$q$; END $body$",
    "ALTER ROLE r PASSWORD 'gizl",
    "CREATE USER MAPPING FOR u SERVER s OPTIONS (user 'u', password 'gizli')",
    "CREATE SUBSCRIPTION s CONNECTION 'host=h password=gizli' PUBLICATION p",
    "ALTER LOGIN [app] WITH PASSWORD = N'gizli'",
]


@pytest.mark.parametrize("sql", UTILITY_CORPUS)
def test_utility_statements_lose_their_values_regardless_of_keep_values(sql):
    for keep in (True, False):
        out = sanitize_stored_query(sql, keep_values=keep)
        assert "gizl" not in out, (keep, out)
        assert sanitize_stored_query(out, keep_values=keep) == out, "idempotent değil"


@pytest.mark.parametrize("sql", PASSWORD_CORPUS)
def test_password_statements_are_never_stored(sql):
    out = sanitize_stored_query(sql, keep_values=True)
    assert PASSWORD_REDACTED_NOTE in out and "gizl" not in out
    # Yalnızca ifade türü kalıyor — rol/kullanıcı adı bile değil.
    assert out.split(" /*")[0] in {"ALTER ROLE", "CREATE ROLE", "CREATE USER", "DO", "CREATE SUBSCRIPTION", "ALTER LOGIN"}
    assert sanitize_stored_query(out, keep_values=True) == out


def test_negative_control_the_old_normalizer_leaks_this_corpus():
    """Kanıtın boş olmadığı. Commit 5 öncesi `slow_query_samples`, `blocking_episodes` ve
    `deadlock_events` metni HAM yazıyordu (yukarıdaki testlerin hepsi kırmızı olurdu); örnekleyici
    `normalize_literals` kullanıyordu ve o da dolar tırnaklı ve kesik dizgide değeri koruyor."""
    corpus = UTILITY_CORPUS + PASSWORD_CORPUS
    assert all("gizl" in sql for sql in corpus)
    leaking = {sql for sql in corpus if "gizl" in normalize_literals(sql)}
    assert leaking == {
        "DO $body$ BEGIN RAISE NOTICE $x$gizli$x$; END $body$",
        "SET x = $v$gizli$v$",
        "SET x = 'gizli",
        "SET x = E'gizli\\'x'",
        "ALTER ROLE r PASSWORD 'gizl",
    }


def test_plannable_statements_follow_the_callers_decision():
    sql = "SELECT * FROM t WHERE a = 'gizli' AND b = 5"
    assert sanitize_stored_query(sql, keep_values=True) == sql  # pg_stat_statements metni
    assert sanitize_stored_query(sql, keep_values=False) == "SELECT * FROM t WHERE a = $1 AND b = $2"
    normalized = "SELECT * FROM t WHERE a = $1 LIMIT $2"
    assert sanitize_stored_query(normalized, keep_values=False) == normalized


@pytest.mark.parametrize("sql", ["SELECT 1", "with x as (select 1) select * from x", "(SELECT 1)",
                                 "/* dbace */ SELECT 1", "(@P1 int)SELECT 1", "<insufficient privilege>", ""])
def test_not_utility(sql):
    assert not is_utility_statement(sql)


def test_do_body_keeps_its_structure():
    out = sanitize_stored_query(
        "DO $$ BEGIN PERFORM 'gizli'; UPDATE marker_probe SET note = note WHERE id = 2; END $$", keep_values=True
    )
    assert out == "DO $$ BEGIN PERFORM $1; UPDATE marker_probe SET note = note WHERE id = $2; END $$"


#: Gerçek bir deadlock log bloğu (PG 17.11, `docker logs`), çok satırlı ifade ve PASSWORD eklenmiş.
DEADLOCK_LOG = (
    "2026-09-17 07:33:14.498 UTC [5189] ERROR:  deadlock detected\n"
    "2026-09-17 07:33:14.498 UTC [5189] DETAIL:  Process 5189 waits for ShareLock on transaction 19839; blocked by process 5190.\n"
    "\tProcess 5190 waits for ShareLock on transaction 19838; blocked by process 5189.\n"
    "\tProcess 5189: DO $$ BEGIN PERFORM 'gizli_dl1'; UPDATE marker_probe SET note = note WHERE id = 2; END $$\n"
    "\tProcess 5190: UPDATE t SET a = 'gizli_x'\n"
    "\tWHERE b = 'gizli_y'\n"
    "2026-09-17 07:33:14.498 UTC [5189] HINT:  See server log for query details.\n"
    '2026-09-17 07:33:14.498 UTC [5189] CONTEXT:  while updating tuple (0,2) in relation "marker_probe"\n'
    "\tSQL statement \"UPDATE marker_probe SET note = 'gizli_ctx'\n"
    '\t  WHERE id = 2"\n'
    "\tPL/pgSQL function inline_code_block line 1 at SQL statement\n"
    "2026-09-17 07:33:14.498 UTC [5189] STATEMENT:  ALTER ROLE r\n"
    "\tPASSWORD 'gizli_pw'"
)


def test_deadlock_log_block_keeps_pids_and_locks_but_no_values():
    out = sanitize_deadlock_detail(DEADLOCK_LOG, source="postgresql_log")
    assert SECRET not in out
    assert "Process 5189 waits for ShareLock on transaction 19839; blocked by process 5190." in out
    assert "\tProcess 5190: UPDATE t SET a = $1\n\tWHERE b = $2" in out
    assert "\tPL/pgSQL function inline_code_block line 1 at SQL statement" in out
    assert f"STATEMENT:  ALTER ROLE /* {PASSWORD_REDACTED_NOTE} */" in out
    assert sanitize_deadlock_detail(out, source="postgresql_log") == out


def test_sqlserver_deadlock_xml_text_nodes_are_sanitized_and_stay_xml():
    xml = "<deadlock><process id='p1' spid='55'><inputbuf>UPDATE t SET a = N'gizli' WHERE id = 5 &amp; x</inputbuf></process></deadlock>"
    out = sanitize_deadlock_detail(xml, source="sqlserver_system_health")
    assert SECRET not in out and "spid='55'" in out and "&amp; x" in out


# --- Yazma yolları -------------------------------------------------------------------------

#: Sorgu metni tutmayan ya da dbace'in TOPLAMADIĞI kolonlar — ve NEDEN.
NOT_COLLECTED = {
    ("alert_rules", "sql_query"): "admin'in yazdığı özel kural SQL'i; hedeften toplanmıyor",
    ("slow_query_samples", "query_class"): "sistem sorgusu SINIF ETİKETİ (classify_system_query) — metin taşımıyor",
}


def guarded_columns() -> dict[str, set[str]]:
    """Modellerden: adı sorgu metni ya da ham ayrıntı olan Text kolonları."""
    out: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Text) and re.search(r"query|raw_detail", column.name):
                if (table.name, column.name) not in NOT_COLLECTED:
                    out.setdefault(table.name, set()).add(column.name)
    return out


def _model_tables() -> dict[str, str]:
    return {mapper.class_.__name__: mapper.class_.__tablename__ for mapper in Base.registry.mappers}


def unsanitized_writes(source: str, filename: str) -> list[str]:
    """Kaynakta guarded kolona arındırıcıdan geçmeden yazılan yerler."""
    columns = guarded_columns()
    tables = _model_tables()
    attr_names = set().union(*columns.values())
    tree = ast.parse(source)
    problems: list[str] = []

    def assigned_from_sanitizer(func: ast.AST | None, name: str) -> bool:
        if func is None:
            return False
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                if "sanitize_" in ast.unparse(node.value):
                    return True
        return False

    def ok(value: ast.AST, func) -> bool:
        if isinstance(value, ast.Constant) and value.value in (None, ""):
            return True
        if "sanitize_" in ast.unparse(value):
            return True
        return isinstance(value, ast.Name) and assigned_from_sanitizer(func, value.id)

    def visit(node: ast.AST, func) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            table = tables.get(callee)
            for kw in node.keywords:
                if table and kw.arg in columns.get(table, ()) and not ok(kw.value, func):
                    problems.append(f"{filename}:{node.lineno} {callee}({kw.arg}=…)")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in attr_names and not ok(node.value, func):
                    problems.append(f"{filename}:{node.lineno} .{target.attr} = …")
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, None)
    return problems


def test_guarded_columns_are_discovered_from_models():
    columns = guarded_columns()
    assert columns["slow_query_samples"] == {"query"}
    assert columns["deadlock_events"] == {"victim_query", "winner_query", "raw_detail"}
    assert "sample_query_text" in columns["wait_query_signatures"]


def test_every_write_to_a_query_text_column_goes_through_the_sanitizer():
    problems = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if "__pycache__" in path.parts or rel in {"models.py", "services/query_text_privacy.py"}:
            continue
        problems += unsanitized_writes(path.read_text(encoding="utf-8"), rel)
    assert not problems, "arındırılmadan yazılan sorgu metni:\n" + "\n".join(problems)


def test_negative_control_the_scanner_finds_raw_writes():
    source = (
        "async def f(session, row, text):\n"
        "    session.add(SlowQuerySample(instance_id=1, query=row['query']))\n"
        "    episode.root_query = text\n"
        "    x = sanitize_stored_query(text, keep_values=False)\n"
        "    ok.victim_query = x\n"
        "    ok2 = BlockingEpisode(root_query=sanitize_stored_query(text, keep_values=False))\n"
    )
    problems = unsanitized_writes(source, "sentetik.py")
    assert problems == ["sentetik.py:2 SlowQuerySample(query=…)", "sentetik.py:3 .root_query = …"]


# --- Geriye dönük temizlik (SQLite, gerçek oturum) --------------------------------------------


async def test_retro_cleanup_sanitizes_existing_rows_once():
    import uuid

    from sqlalchemy import select

    from app.database import SessionLocal, init_db
    from app.models import AppSetting, BlockingEpisode, DeadlockEvent, Instance, SlowQuerySample
    from app.services.credentials import encrypt_secret
    from app.services.query_text_privacy import CLEANUP_VERSION_KEY

    await init_db()
    async with SessionLocal() as session:
        instance = Instance(name=f"cleanup-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
                            database="d", username="u", password=encrypt_secret("x"))
        session.add(instance)
        await session.flush()
        plain = SlowQuerySample(instance_id=instance.id, queryid="1", query="SELECT * FROM t WHERE a = $1")
        secret = SlowQuerySample(instance_id=instance.id, queryid="2", query="ALTER ROLE r PASSWORD 'gizli'")
        episode = BlockingEpisode(instance_id=instance.id, started_at=__import__("datetime").datetime.now(),
                                  root_pid=1, root_query="SET application_name = 'gizli'")
        dead = DeadlockEvent(instance_id=instance.id, detected_at=__import__("datetime").datetime.now(),
                             source="postgresql_log", victim_query="DO $$ BEGIN PERFORM 'gizli'; END $$",
                             raw_detail=DEADLOCK_LOG)
        session.add_all([plain, secret, episode, dead])
        setting = await session.get(AppSetting, CLEANUP_VERSION_KEY)
        if setting is not None:
            await session.delete(setting)
        await session.commit()
        ids = plain.id, secret.id, episode.id, dead.id

    async with SessionLocal() as session:
        changed = await run_stored_text_cleanup(session)
    assert changed["slow_query_samples"] >= 1 and changed["blocking_episodes"] >= 1 and changed["deadlock_events"] >= 1

    async with SessionLocal() as session:
        rows = [
            await session.get(SlowQuerySample, ids[0]), await session.get(SlowQuerySample, ids[1]),
            await session.get(BlockingEpisode, ids[2]), await session.get(DeadlockEvent, ids[3]),
        ]
        assert rows[0].query == "SELECT * FROM t WHERE a = $1"
        assert rows[1].query == f"ALTER ROLE /* {PASSWORD_REDACTED_NOTE} */"
        assert rows[2].root_query == "SET application_name = $1"
        assert SECRET not in rows[3].victim_query and SECRET not in rows[3].raw_detail
        assert (await session.execute(select(AppSetting.value).where(AppSetting.key == CLEANUP_VERSION_KEY))).scalar() == "1"
        # İkinci çalıştırma: sürüm anahtarı var → hiçbir şey taranmıyor.
        assert await run_stored_text_cleanup(session) == {}
