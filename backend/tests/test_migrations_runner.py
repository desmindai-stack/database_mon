"""Migration uygulayıcısı: işlem dışı çalışma, parçalı ifade, geçersiz index kurtarma (Faz 31 Commit 10b).

Sahte bağlantı, hangi ifadenin işlem İÇİNDE mi DIŞINDA mı çalıştığını ve hangi parametrelerle çalıştığını kaydeder;
gerçek PostgreSQL'de aynı davranışlar `test_migration_scale_live_postgres.py` ve on-prem paket testinde sınanıyor.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from app import migrations_runner
from app.migrations_runner import StatementStat, apply_migrations, needs_autocommit, parse_args_for_test
from app.migration_sql import split_statements


class FakeConn:
    def __init__(self, bounds=(1, 120_001), invalid_indexes=(), fail_on_chunk: int | None = None) -> None:
        self.bounds = bounds
        self.invalid_indexes = set(invalid_indexes)
        self.fail_on_chunk = fail_on_chunk
        self.calls: list[dict] = []
        self.in_tx = False
        self.recorded: list[str] = []
        self.chunks_seen = 0

    @contextlib.asynccontextmanager
    async def transaction(self):
        self.in_tx = True
        try:
            yield
        finally:
            self.in_tx = False

    async def execute(self, sql: str, *args):
        if sql.startswith("INSERT INTO dbace_meta.applied_migrations"):
            self.recorded.append(args[0])
        if args and "$1" in sql and "$2" in sql:
            self.chunks_seen += 1
            if self.fail_on_chunk is not None and self.chunks_seen == self.fail_on_chunk:
                raise RuntimeError("bağlantı koptu")
        self.calls.append({"sql": sql, "args": args, "in_tx": self.in_tx})
        return "UPDATE 7" if sql.lstrip().upper().startswith("UPDATE") else "OK"

    async def fetch(self, sql: str, *args):
        return []

    async def fetchrow(self, sql: str, *args):
        if "min(id)" in sql:
            lo, hi = self.bounds
            return {"lo": lo, "hi": hi}
        if "indisvalid" in sql:
            return {"relname": args[0]} if args[0] in self.invalid_indexes else None
        return None


def write(tmp_path: Path, name: str, sql: str) -> Path:
    (tmp_path / name).write_text(sql, encoding="utf-8")
    return tmp_path


CHUNKED = """-- dbace:chunked big_t 50000
UPDATE big_t SET a = 1 WHERE id >= $1 AND id < $2 AND a IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_big_a ON big_t (a);
"""


async def test_file_with_concurrently_or_chunks_runs_outside_a_transaction_and_is_recorded_last(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = FakeConn()
    applied = await apply_migrations(conn, directory)
    assert applied == ["20260101000000_a.sql"] and conn.recorded == ["20260101000000_a.sql"]
    work = [c for c in conn.calls if "big_t" in c["sql"]]
    assert work and not any(c["in_tx"] for c in work), "işlem dışı çalışmalı"
    assert conn.calls[-1]["sql"].startswith("INSERT INTO dbace_meta.applied_migrations"), "kayıt EN SON"


async def test_plain_file_still_runs_whole_in_one_transaction(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", "CREATE TABLE IF NOT EXISTS t (id int);\nCREATE INDEX ix ON t (id);\n")
    conn = FakeConn()
    await apply_migrations(conn, directory)
    body = [c for c in conn.calls if "CREATE TABLE IF NOT EXISTS t " in c["sql"]]
    assert len(body) == 1 and body[0]["in_tx"] is True, "tek işlemde ve tek çağrıda"
    assert not needs_autocommit(split_statements("SELECT 1;"))


async def test_chunk_ranges_cover_min_to_max_in_order_with_exclusive_upper_bounds(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = FakeConn(bounds=(1, 120_001))
    await apply_migrations(conn, directory)
    ranges = [c["args"] for c in conn.calls if c["sql"].startswith("UPDATE big_t")]
    assert ranges == [(1, 50_001), (50_001, 100_001), (100_001, 150_001)]
    # 120 001 sayısı üçüncü aralığa düşüyor: hiçbir satır dışarıda kalmıyor.
    assert ranges[-1][0] <= 120_001 < ranges[-1][1]


async def test_single_row_table_gets_exactly_one_chunk_and_empty_table_none(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED.replace("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_big_a ON big_t (a);", ""))
    one = FakeConn(bounds=(5, 5))
    await apply_migrations(one, directory)
    assert [c["args"] for c in one.calls if c["sql"].startswith("UPDATE big_t")] == [(5, 50_005)]

    empty = FakeConn(bounds=(None, None))
    await apply_migrations(empty, directory)
    assert not [c for c in empty.calls if c["sql"].startswith("UPDATE big_t")], "boş tabloda parça çalışmamalı"
    assert empty.recorded, "boş tablo migration'ı başarısız yapmaz"


async def test_invalid_table_name_in_the_marker_is_rejected(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql",
                      '-- dbace:chunked big_t;drop 10\nUPDATE big_t SET a = 1 WHERE id >= $1 AND id < $2 AND a IS NULL;')
    # İşaret tanınmaz (tablo adı deseni), dolayısıyla ifade parçalı sayılmaz ve düz çalışır: enjeksiyon yolu yok.
    statements = split_statements((directory / "20260101000000_a.sql").read_text(encoding="utf-8"))
    assert all(not s.chunked for s in statements)


async def test_failure_midway_leaves_the_file_unrecorded_and_a_rerun_completes_it(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    broken = FakeConn(fail_on_chunk=2)
    with pytest.raises(RuntimeError):
        await apply_migrations(broken, directory)
    assert broken.recorded == [], "yarım dosya kayda GEÇMEMELİ"
    assert len([c for c in broken.calls if c["sql"].startswith("UPDATE big_t")]) == 1, "ilk parça çalıştı, ikinci kesildi"

    rerun = FakeConn()
    assert await apply_migrations(rerun, directory) == ["20260101000000_a.sql"]
    assert rerun.recorded == ["20260101000000_a.sql"] and len([c for c in rerun.calls if c["sql"].startswith("UPDATE big_t")]) == 3


async def test_an_invalid_index_left_by_an_interrupted_build_is_dropped_before_recreating(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_big_a ON big_t (a);\n")
    conn = FakeConn(invalid_indexes={"ix_big_a"})
    await apply_migrations(conn, directory)
    sqls = [c["sql"] for c in conn.calls]
    drop = next(i for i, s in enumerate(sqls) if s.startswith("DROP INDEX CONCURRENTLY"))
    create = next(i for i, s in enumerate(sqls) if s.startswith("CREATE INDEX CONCURRENTLY"))
    assert drop < create and '"ix_big_a"' in sqls[drop]


async def test_a_valid_or_missing_index_is_not_dropped(tmp_path):
    """NEGATİF KONTROL: geçerli index'e dokunulmaz."""
    directory = write(tmp_path, "20260101000000_a.sql", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_big_a ON big_t (a);\n")
    conn = FakeConn(invalid_indexes=set())
    await apply_migrations(conn, directory)
    assert not [c for c in conn.calls if c["sql"].startswith("DROP INDEX")]


async def test_do_block_in_a_concurrently_file_is_sent_whole_not_split_at_semicolons(tmp_path):
    sql = ("DO $$ BEGIN PERFORM 1; PERFORM 2; END $$;\n"
           "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_big_a ON big_t (a);\n")
    conn = FakeConn()
    await apply_migrations(conn, write(tmp_path, "20260101000000_a.sql", sql))
    do_calls = [c["sql"] for c in conn.calls if c["sql"].startswith("DO")]
    assert do_calls == ["DO $$ BEGIN PERFORM 1; PERFORM 2; END $$"]


async def test_only_until_and_no_record_options(tmp_path):
    for name in ("20260101000000_a.sql", "20260102000000_b.sql", "20260103000000_c.sql"):
        write(tmp_path, name, f"SELECT '{name}';\n")
    conn = FakeConn()
    assert await apply_migrations(conn, tmp_path, until="20260103000000_c.sql") == [
        "20260101000000_a.sql", "20260102000000_b.sql"]
    conn = FakeConn()
    assert await apply_migrations(conn, tmp_path, only="20260102000000_b.sql") == ["20260102000000_b.sql"]
    conn = FakeConn()
    assert await apply_migrations(conn, tmp_path, only="20260102000000_b.sql", record=False) == ["20260102000000_b.sql"]
    assert conn.recorded == [] and not [c for c in conn.calls if "dbace_meta" in c["sql"]], \
        "--no-record kayıt şemasına/tablosuna HİÇ dokunmamalı (bulut veritabanı kirlenmesin)"


async def test_statement_timeout_is_set_and_validated(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", "SELECT 1;\n")
    conn = FakeConn()
    await apply_migrations(conn, directory, statement_timeout="8s")
    assert conn.calls[0]["sql"] == "SET statement_timeout = '8s'"
    with pytest.raises(ValueError):
        await apply_migrations(FakeConn(), directory, statement_timeout="8s'; DROP TABLE x; --")


async def test_stats_record_every_chunk_with_rows_and_time(tmp_path):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    stats: list[StatementStat] = []
    await apply_migrations(FakeConn(), directory, stats=stats)
    chunks = [s for s in stats if s.kind == "chunk"]
    assert len(chunks) == 3 and all(s.rows == 7 and s.seconds >= 0 for s in chunks)
    assert any(s.kind == "statement" and "CREATE INDEX" in s.text for s in stats)


def test_command_line_options():
    args = parse_args_for_test(["/mig", "--only", "x.sql", "--no-record", "--statement-timeout", "8s"])
    assert (args.directory, args.only, args.no_record, args.statement_timeout) == ("/mig", "x.sql", True, "8s")
    defaults = parse_args_for_test([])
    assert defaults.directory == "/app/migrations" and defaults.only is None and defaults.no_record is False
    assert migrations_runner._rows("UPDATE 12345") == 12345 and migrations_runner._rows("CREATE INDEX") == 0
