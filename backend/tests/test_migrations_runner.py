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


async def test_chunk_progress_log_line_matches_the_shared_regex_other_tests_import(tmp_path, caplog):
    """Log satırı ve onu okuyan testler (`test_onprem_package_live.py`) AYNI `CHUNK_PROGRESS_LOG_RE`'den
    besleniyor (Faz 31 Commit 10g) — biri log metnini değiştirip diğerini unutursa bu test HIZLI ve HER
    ZAMAN AÇIK olduğu için sessizce ayrışamaz; canlı on-prem testinin (yavaş, DBACE_TEST_ONPREM=1 gerektirir)
    çok daha geç yakalayacağı bir hatayı burada, saniyeler içinde yakalar."""
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = FakeConn()
    with caplog.at_level("INFO", logger="app.migrations_runner"):
        await apply_migrations(conn, directory)
    progress_lines = [r.message for r in caplog.records if "parça" in r.message]
    assert progress_lines, "en az bir ilerleme satırı loglanmalı (son parça her zaman loglanır)"
    for line in progress_lines:
        assert migrations_runner.CHUNK_PROGRESS_LOG_RE.search(line), f"biçimle eşleşmedi: {line!r}"


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


# --- Parça süre bütçesi: iptal edilen parça YARI genişlikle yeniden denenir (Faz 31 Commit 10d madde A3) ------
#
# Gerçek CI diskini birebir taklit eden bir ortamda (cgroup yazma kısıtı + 0,35 CPU; #53 toplamı CI'da 49,7 sn,
# taklitte 55,6 sn) 8 bin'lik parçaların çoğu 0,9 sn sürerken TEK parça 4,5–5,2 sn sürdü (checkpoint/fsync
# birikimi) — parça boyunu düşürmek bu sıçramayı kaldırmıyor. Çözüm boyu değil DAVRANIŞ: bütçeyi aşan parça iptal
# edilip yarı genişlikle yeniden deneniyor (idempotent), hiçbir parça bütçenin üstünde BAŞARIYLA bitmiyor.


class BudgetConn(FakeConn):
    """`max_width`ten geniş her parçayı sunucu iptal ediyormuş gibi davranır; `statement_timeout` oturum ayarı tutar."""

    def __init__(self, *, max_width: int, timeout: str | None = "8s", **kwargs) -> None:
        super().__init__(**kwargs)
        self.max_width = max_width
        self.setting = timeout
        self.timeouts_set: list[str] = []

    async def fetchrow(self, sql: str, *args):
        if "current_setting('statement_timeout')" in sql:
            return {"v": self.setting} if self.setting is not None else None
        return await super().fetchrow(sql, *args)

    async def execute(self, sql: str, *args):
        if sql.startswith("SET statement_timeout"):
            value = sql.split("'")[1]
            self.timeouts_set.append(value)
            self.setting = value
            return "SET"
        if args and "$1" in sql and "$2" in sql and args[1] - args[0] > self.max_width:
            from asyncpg.exceptions import QueryCanceledError

            self.cancelled = getattr(self, "cancelled", []) + [(args[0], args[1])]
            raise QueryCanceledError("canceling statement due to statement timeout")
        return await super().execute(sql, *args)


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(migrations_runner, "CHUNK_RETRY_PAUSE_SECONDS", 0)


def _updates(conn: FakeConn) -> list[tuple[int, int]]:
    return [c["args"] for c in conn.calls if c["sql"].startswith("UPDATE big_t")]


async def test_a_cancelled_chunk_is_halved_and_retried_and_every_id_is_still_covered_exactly_once(tmp_path, no_pause):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = BudgetConn(max_width=12_500, bounds=(1, 120_001))
    stats: list[StatementStat] = []
    await apply_migrations(conn, directory, stats=stats)
    ranges = _updates(conn)
    # 50 000 → 25 000 → 12 500: yalnızca 12 500'lük parçalar BAŞARIYLA tamamlandı, aralıklar ardışık ve çakışmasız.
    assert all(end - start <= 12_500 for start, end in ranges)
    assert ranges[0][0] == 1 and all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])), "boşluk/çakışma yok"
    assert ranges[-1][1] > 120_001, "son id de kapsandı"
    retries = [s for s in stats if s.kind == "retry"]
    assert len(retries) == 2, "50 000→25 000→12 500: iki iptal"
    assert len([s for s in stats if s.kind == "chunk"]) == len(ranges)
    assert conn.recorded, "dosya başarıyla bitti ve kayda geçti"


async def test_the_session_statement_timeout_gives_a_third_as_chunk_budget_and_is_restored(tmp_path, no_pause):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = BudgetConn(max_width=10**9, timeout="8s", bounds=(1, 50_000))
    await apply_migrations(conn, directory)
    # CHUNKED dosyası hem parçalı UPDATE hem CONCURRENTLY index içeriyor (#53'ün ta kendisi): parça bütçesi
    # 8 sn'nin 1/3'ü ile başlayıp özgün ayara döner, SONRA CONCURRENTLY kendi payını sınırsız (Faz 31 Commit
    # 10f) çalıştırıp AYNI özgün ayara döner — ikisi de kendi bölümünü geri bırakıyor.
    assert conn.timeouts_set == ["2666ms", "8s", "0", "8s"], "parça bütçesi VE CONCURRENTLY'nin sınırsız payı ayrı ayrı geri konur"


async def test_an_explicit_budget_overrides_and_no_known_limit_means_no_timeout_change(tmp_path, no_pause):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    explicit = BudgetConn(max_width=10**9, timeout="8s", bounds=(1, 50_000))
    await apply_migrations(explicit, directory, chunk_budget_seconds=0.5)
    assert explicit.timeouts_set[0] == "500ms"
    unlimited = BudgetConn(max_width=10**9, timeout="0", bounds=(1, 50_000))
    await apply_migrations(unlimited, directory)
    # Parçalı UPDATE için sınır yoksa oturum ayarına dokunulmaz (eski davranış) — ama CONCURRENTLY kendi payını
    # HER ZAMAN sınırsız çalıştırır (Faz 31 Commit 10f), oturumun zaten sınırsız olmasından bağımsız.
    assert unlimited.timeouts_set == ["0", "0"], "CONCURRENTLY kendi sınırsız payını sınır zaten yokken de set eder"
    assert len(_updates(unlimited)) == 1


async def test_negative_control_without_cancellations_the_width_never_shrinks(tmp_path, no_pause):
    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = BudgetConn(max_width=10**9, bounds=(1, 120_001))
    stats: list[StatementStat] = []
    await apply_migrations(conn, directory, stats=stats)
    assert _updates(conn) == [(1, 50_001), (50_001, 100_001), (100_001, 150_001)]
    assert not [s for s in stats if s.kind == "retry"]


async def test_negative_control_a_server_too_slow_for_the_narrowest_chunk_fails_loudly(tmp_path, no_pause):
    from asyncpg.exceptions import QueryCanceledError

    directory = write(tmp_path, "20260101000000_a.sql", CHUNKED)
    conn = BudgetConn(max_width=migrations_runner.MIN_CHUNK_SIZE - 1, bounds=(1, 120_001))
    with pytest.raises(QueryCanceledError):
        await apply_migrations(conn, directory)
    assert not conn.recorded, "başarısız dosya kayda GEÇMEZ"
    assert conn.setting == "8s", "hata olsa da oturum ayarı geri konur"


def test_timeout_setting_parser_reads_postgres_show_output():
    parse = migrations_runner._timeout_seconds
    assert parse("8s") == 8.0 and parse("2666ms") == pytest.approx(2.666) and parse("1min") == 60.0
    assert parse("0") is None and parse(None) is None and parse("garip") is None
