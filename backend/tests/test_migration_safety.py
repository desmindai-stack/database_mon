"""Büyük tabloya dokunan migration'ların güvenliği: parçalı ve CONCURRENTLY (Faz 31 Commit 10b).

Arka plan: #53, 393 bin satırlık `slow_query_samples`'ı tek UPDATE ile dolduruyor ve index'i CONCURRENTLY kurmuyordu;
Supabase'de zaman aşımına uğradı ve tümüyle geri alındı. Bu dosya kuralı CI'a bağlıyor — ELLE LİSTE YOK:

- "büyük tablo" = saklama politikasının temizlediği tablolar + günlük toplulaştırma tabloları (`large_tables()`, koddan);
- denetlenen dosyalar = `supabase/migrations/*.sql`'in TAMAMI (dizin taranıyor);
- DEPLOY.md'nin "uzun süren migration'lar" tablosu, dosyalardan HESAPLANAN kümeyle karşılaştırılıyor.

Kuralın kendisinin çalıştığı NEGATİF KONTROLLERLE gösteriliyor: kuralı bozan her SQL ayrı ayrı yakalanıyor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import migration_sql
from app.migration_sql import (
    MAX_CHUNK_SIZE,
    analyze_directory,
    analyze_sql,
    large_tables,
    long_running_tables,
    split_statements,
    touched_large_tables,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
DEPLOY = ROOT / "DEPLOY.md"
LARGE = {"big_t", "other_big"}


def rules(sql: str, large: set[str] = LARGE) -> list[str]:
    return sorted(v.rule for v in analyze_sql("x.sql", sql, large))


# --- Gerçek migration dizini -----------------------------------------------------------------------


def test_no_migration_touches_a_large_table_unsafely():
    violations = analyze_directory(MIGRATIONS, large_tables())
    assert violations == [], "\n" + "\n".join(str(v) for v in violations)


def test_the_scan_really_covers_every_migration_file():
    files = list(MIGRATIONS.glob("*.sql"))
    assert len(files) >= 50
    # Her dosya ayrıştırılabiliyor ve en az bir ifade veriyor (boş dosya = sessizce atlanmış tarama olurdu).
    for path in files:
        assert split_statements(path.read_text(encoding="utf-8")), path.name


def test_large_tables_are_derived_from_code_not_from_a_list(monkeypatch):
    from app.models import MetricRollupDaily, SlowQuerySample
    from app.services import retention

    tables = large_tables()
    assert {"slow_query_samples", "metric_samples", "alert_events", "captured_plans"} <= tables
    assert MetricRollupDaily.__tablename__ in tables
    assert tables == {m.__tablename__ for m, _ in retention.RETENTION_TARGETS} | {
        "metric_rollup_daily", "schema_object_daily_samples"}
    # NEGATİF KONTROL: saklama listesine giren yeni bir tablo OTOMATİK olarak büyük sayılır.
    class NewSeries:  # noqa: D401
        __tablename__ = "yeni_zaman_serisi"

    monkeypatch.setattr(retention, "RETENTION_TARGETS", (*retention.RETENTION_TARGETS, (NewSeries, SlowQuerySample.collected_at)))
    assert "yeni_zaman_serisi" in large_tables()


def test_retention_still_deletes_every_table_in_the_shared_list():
    """Liste iki işi görüyor (silme + büyük tablo tanımı): silme döngüsü aynı listeyi kullanıyor."""
    source = (ROOT / "backend" / "app" / "services" / "retention.py").read_text(encoding="utf-8")
    assert "for model, ts_column in RETENTION_TARGETS:" in source


# --- Kural: index --------------------------------------------------------------------------------


def test_plain_create_index_on_a_large_table_is_flagged():
    assert rules("CREATE INDEX IF NOT EXISTS ix_a ON big_t (a);") == ["index-concurrently"]


def test_concurrent_index_on_a_large_table_is_allowed():
    assert rules("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_a ON big_t (a);") == []
    assert rules("CREATE UNIQUE INDEX CONCURRENTLY ix_a ON big_t (a);") == []


def test_index_on_a_small_table_or_a_table_created_in_the_same_file_is_allowed():
    assert rules("CREATE INDEX ix_a ON small_t (a);") == []
    assert rules("CREATE TABLE IF NOT EXISTS big_t (id bigserial primary key, a int);\nCREATE INDEX ix_a ON big_t (a);") == []


# --- Kural: UPDATE/DELETE -----------------------------------------------------------------------


GOOD_CHUNK = """-- dbace:chunked big_t 50000
UPDATE big_t SET a = 1 WHERE id >= $1 AND id < $2 AND a IS NULL;"""


def test_a_chunked_idempotent_update_is_allowed():
    assert rules(GOOD_CHUNK) == []
    assert rules(GOOD_CHUNK.replace("UPDATE big_t SET a = 1", "DELETE FROM big_t").replace("SET a = 1", "")) == []


def test_unchunked_update_or_delete_on_a_large_table_is_flagged():
    assert rules("UPDATE big_t SET a = 1 WHERE a IS NULL;") == ["chunked"]
    assert rules("DELETE FROM big_t WHERE a IS NULL;") == ["chunked"]
    # #53'ün ilk sürümü: tek UPDATE (regresyon kontrolü)
    assert rules("UPDATE big_t SET a = 1 WHERE query_hash IS NULL;") == ["chunked"]


def test_chunk_marker_rules():
    assert rules(GOOD_CHUNK.replace("big_t 50000", "other_big 50000")) == ["chunked-table"]
    assert rules(GOOD_CHUNK.replace("50000", "0")) == ["chunked-size"]
    assert rules(GOOD_CHUNK.replace("50000", str(MAX_CHUNK_SIZE + 1))) == ["chunked-size"]
    assert rules(GOOD_CHUNK.replace("id >= $1 AND id < $2", "a > 0")) == ["chunked-range"]
    assert rules(GOOD_CHUNK.replace(" AND a IS NULL", "")) == ["chunked-idempotent"]


def test_marker_applies_only_to_the_next_statement():
    sql = GOOD_CHUNK + "\nUPDATE big_t SET b = 2 WHERE b IS NULL;"
    assert rules(sql) == ["chunked"]


def test_update_on_a_small_table_is_allowed():
    assert rules("UPDATE small_t SET a = 1;") == []


def test_write_to_a_large_table_inside_a_do_block_is_flagged():
    sql = "DO $$ BEGIN UPDATE big_t SET a = 1; END $$;"
    assert rules(sql) == ["inside-body"]
    assert rules("CREATE FUNCTION f() RETURNS void LANGUAGE sql AS $body$ DELETE FROM big_t $body$;") == ["inside-body"]
    assert rules("DO $$ BEGIN UPDATE small_t SET a = 1; END $$;") == []


# --- Kural: tabloyu yeniden yazan / tarayan ALTER'lar -----------------------------------------


@pytest.mark.parametrize("sql,expected", [
    ("ALTER TABLE big_t ALTER COLUMN a TYPE bigint;", ["rewrite"]),
    ("ALTER TABLE big_t ALTER COLUMN a SET NOT NULL;", ["full-scan"]),
    ("ALTER TABLE big_t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES p(id);", ["constraint-not-valid"]),
    ("ALTER TABLE big_t ADD COLUMN g BIGINT REFERENCES p(id);", ["constraint-not-valid"]),
    ("ALTER TABLE big_t ADD COLUMN c INT CHECK (c > 0);", ["constraint-not-valid"]),
    ("ALTER TABLE big_t ADD COLUMN u uuid DEFAULT gen_random_uuid();", ["volatile-default"]),
    ("ALTER TABLE big_t ADD COLUMN r DOUBLE PRECISION DEFAULT random();", ["volatile-default"]),
    ("VACUUM FULL big_t;", ["heavy-lock"]),
    ("LOCK TABLE big_t IN ACCESS EXCLUSIVE MODE;", ["heavy-lock"]),
])
def test_table_rewriting_or_scanning_alters_are_flagged(sql, expected):
    assert rules(sql) == expected


@pytest.mark.parametrize("sql", [
    "ALTER TABLE big_t ADD COLUMN IF NOT EXISTS a BIGINT;",
    "ALTER TABLE big_t ADD COLUMN flag BOOLEAN NOT NULL DEFAULT FALSE;",        # sabit varsayılan: yalnızca katalog
    "ALTER TABLE big_t ADD COLUMN created TIMESTAMPTZ DEFAULT now();",         # STABLE: yeniden yazma yok (PG 11+)
    "ALTER TABLE big_t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES p(id) NOT VALID;",
    "ALTER TABLE big_t VALIDATE CONSTRAINT fk;",
    "ALTER TABLE big_t DROP COLUMN old;",
    "ALTER TABLE small_t ALTER COLUMN a TYPE bigint;",
])
def test_metadata_only_alters_are_allowed(sql):
    assert rules(sql) == []


# --- Ayrıştırıcı ---------------------------------------------------------------------------------


def test_comments_and_string_literals_do_not_create_findings():
    sql = "-- CREATE INDEX ix ON big_t (a);\n/* UPDATE big_t SET a = 1; */\nSELECT 'UPDATE big_t SET a = 1;' AS note;"
    assert rules(sql) == []
    assert [s.sql for s in split_statements(sql)] == ["SELECT 'UPDATE big_t SET a = 1;' AS note"]


def test_splitter_respects_dollar_quotes_strings_and_escape_strings():
    sql = ("DO $$ BEGIN PERFORM 1; PERFORM 2; END $$;\n"
           "SELECT E'it\\'s; fine', 'a;b', $tag$x;y$tag$;\n"
           "SELECT 3;")
    parts = split_statements(sql)
    assert len(parts) == 3
    assert parts[0].sql.startswith("DO $$") and parts[0].sql.endswith("$$")
    assert "it\\'s; fine" in parts[1].sql and "$tag$x;y$tag$" in parts[1].sql


def test_splitter_reports_line_numbers_and_binds_the_marker_across_comments_and_blank_lines():
    sql = "SELECT 1;\n\n-- dbace:chunked t 10\n-- açıklama\n\nUPDATE t SET a = 1 WHERE id >= $1 AND id < $2 AND a IS NULL;\nSELECT 2;"
    parts = split_statements(sql)
    assert [p.line for p in parts] == [1, 6, 7]
    assert parts[1].chunk == ("t", 10) and parts[0].chunk is None and parts[2].chunk is None


def test_parameters_and_dollar_quote_lookalikes_are_not_mistaken_for_dollar_quotes():
    sql = "UPDATE t SET a = $1 WHERE id >= $2;\nSELECT 1;"
    assert len(split_statements(sql)) == 2


# --- DEPLOY.md: uzun süren migration'lar ---------------------------------------------------------


def _long_running_files() -> dict[str, set[str]]:
    large = large_tables()
    found = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        tables = long_running_tables(path.read_text(encoding="utf-8"), large)
        if tables:
            found[path.name] = tables
    return found


def _deploy_section() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index("## Uzun süren migration'lar ve bakım penceresi")
    end = text.find("\n## ", start + 10)
    return text[start:end if end != -1 else None]


def test_deploy_lists_every_long_running_migration_with_duration_and_maintenance_window():
    section = _deploy_section()
    missing, incomplete = [], []
    for name in _long_running_files():
        rows = [line for line in section.splitlines() if name in line and line.lstrip().startswith("|")]
        if not rows:
            missing.append(name)
        elif not re.search(r"\d", rows[0]) or "bakım penceresi" not in rows[0].lower():
            incomplete.append(name)
    assert not missing, f"DEPLOY.md 'Uzun süren migration'lar' tablosunda yok: {missing}"
    assert not incomplete, f"süre (sayı) ve bakım penceresi bilgisi eksik: {incomplete}"


def test_deploy_table_lists_no_file_that_is_not_long_running():
    """NEGATİF KONTROL yönü: tabloda olup dosyalardan hesaplanan kümede olmayan satır, eskimiş bilgi demek."""
    long_running = set(_long_running_files())
    listed = set(re.findall(r"`(\d{14}_[a-z0-9_]+\.sql)`", _deploy_section()))
    assert listed <= long_running, f"DEPLOY tablosunda uzun süren olmayan dosya: {listed - long_running}"


def test_the_check_notices_a_new_long_running_migration(tmp_path):
    """NEGATİF KONTROL: yeni bir migration büyük tabloda index kuruyorsa 'uzun süren' kümesine girer."""
    sql = "CREATE INDEX CONCURRENTLY ix_new ON slow_query_samples (queryid);"
    assert long_running_tables(sql, large_tables()) == {"slow_query_samples"}
    assert touched_large_tables("ALTER TABLE slow_query_samples ADD COLUMN x INT;", large_tables()) == {"slow_query_samples"}
    assert long_running_tables("ALTER TABLE slow_query_samples ADD COLUMN x INT;", large_tables()) == set()


def test_module_docstring_syntax_matches_the_runner():
    """Belgedeki söz dizimi ile ayrıştırıcının tanıdığı aynı."""
    assert migration_sql.CHUNK_MARKER.match("-- dbace:chunked slow_query_samples 50000")
    assert not migration_sql.CHUNK_MARKER.match("-- dbace:batched")
