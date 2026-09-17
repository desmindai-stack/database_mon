"""Migration dosyalarının adı, sırası ve DEPLOY.md tablosu (Faz 31 Commit 4).

NEDEN: migration'lar Supabase SQL Editor'de DOSYA ADI SIRASIYLA elle çalıştırılıyor. Faz 28–31
boyunca yedi dosya gelecek tarihle adlandırılmıştı (20260917…20260923, bugün 2026-09-16).
Gelecek tarihli bir ad iki şeyi bozar: sonradan eklenen ve bugünün tarihini taşıyan bir
migration sıralamada ONLARIN ÖNÜNE düşer (bağımlılık sırası tersine döner), ve `supabase db
push` kullanan bir ortamda sürüm geçmişi gerçek zamanla ilişkisiz hâle gelir.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
DEPLOY = ROOT / "DEPLOY.md"

_NAME = re.compile(r"^(\d{14})_[a-z0-9_]+\.sql$")


def _files() -> list[str]:
    return sorted(p.name for p in MIGRATIONS.glob("*.sql"))


def test_every_migration_name_is_a_valid_timestamp_and_snake_case():
    bad = [name for name in _files() if not _NAME.match(name)]
    assert not bad, f"ad kuralına uymayan migration'lar: {bad}"
    for name in _files():
        datetime.strptime(name[:14], "%Y%m%d%H%M%S")  # geçersiz tarih ValueError verir


def test_no_migration_is_dated_in_the_future():
    today = datetime.now(UTC).strftime("%Y%m%d")
    future = [name for name in _files() if name[:8] > today]
    assert not future, (
        f"gelecek tarihli migration'lar (bugün {today}): {future}. Sonradan eklenen bugünkü bir "
        "migration bunların önüne sıralanır."
    )


def test_timestamps_are_unique_so_the_order_is_unambiguous():
    stamps = [name[:14] for name in _files()]
    duplicates = sorted({s for s in stamps if stamps.count(s) > 1})
    assert not duplicates, f"aynı zaman damgasını taşıyan migration'lar: {duplicates}"


def test_deploy_table_lists_every_migration_once_in_directory_order_with_consecutive_numbers():
    rows = re.findall(r"^\| (\d+) \| `([^`]+\.sql)` \|", DEPLOY.read_text(encoding="utf-8"), re.MULTILINE)
    assert [name for _, name in rows] == _files(), "DEPLOY.md migration tablosu dizinle aynı değil"
    assert [int(n) for n, _ in rows] == list(range(1, len(rows) + 1)), "tablo numaraları ardışık değil"


def test_every_postgres_added_column_is_also_added_to_existing_sqlite_databases():
    """`create_all` var olan tabloya kolon EKLEMEZ; yerel SQLite'ta bunu `migrate_schema` yapıyor.

    Faz 31 Commit 3'te dört kolonun migration'ı yazıldı ama `migrate_schema`'ya eklenmedi:
    testler sıfırdan kurulan veritabanında geçti, var olan yerel veritabanı kırılırdı.
    """
    src = (ROOT / "backend" / "app" / "database.py").read_text(encoding="utf-8")
    mirrored = {
        (t.lower(), c.lower())
        for t, c in re.findall(r'_sqlite_add_column_if_missing\(\s*conn,\s*"(\w+)",\s*"(\w+)"', src)
    }
    missing = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for table, column in re.findall(
            r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+)\s+ADD COLUMN\s+IF NOT EXISTS\s+(\w+)",
            path.read_text(encoding="utf-8"),
            re.IGNORECASE,
        ):
            if (table.lower(), column.lower()) not in mirrored:
                missing.append(f"{path.name}: {table}.{column}")
    assert not missing, "migrate_schema'da karşılığı olmayan kolonlar:\n" + "\n".join(missing)
