"""`supabase/migrations/*.sql` dosyalarını ad sırasıyla, bir kez uygular (Faz 31 Commit 8; parçalama Commit 10b).

## Neden

On-prem paketi yeni kurulumda initdb'ye yalnızca ilk DÖRT migration'ı bağlıyordu, ardından uygulama
`create_all` çalıştırıyordu. `create_all` var olan tabloya kolon eklemiyor ve `migrate_schema`
PostgreSQL'de no-op: yeni kurulum 60 kolon eksik açılıyordu (Commit 5'te ölçüldü). Yükseltmede ise
migration'ları elle uygulamak kurulum yapanın hafızasına kalıyordu.

## Nasıl

- Uygulanan sürümler `dbace_meta.applied_migrations` tablosunda. Ayrı şema: uygulamanın `public`
  şeması modellerle birebir kalsın (şema eşliği testi `public`'i karşılaştırıyor).
- Her dosya kendi işleminde; hata olursa o dosya geri alınıyor ve çalıştırıcı DURUYOR (sonraki
  migration'lar öncekine dayanabilir).
- **İşlemsiz dosyalar:** `CREATE INDEX CONCURRENTLY` içeren ya da `-- dbace:chunked` işaretli ifadesi olan dosya işlem
  bloğunda çalışmaz; ifadeler `app/migration_sql.py` ayrıştırıcısıyla (dolar-tırnaklı gövdelere ve string'lere saygılı)
  tek tek, kendi otomatik commit'iyle çalışır. Dosya BAŞTAN SONA başarıyla bitince kayda geçer; yarıda kesilirse yeniden
  çalıştırmak güvenli (ifadeler idempotent).
- **Parçalı ifade** (Commit 10b): `-- dbace:chunked <tablo> <boy>` işaretli UPDATE/DELETE, tablonun `min(id)..max(id)`
  aralığında `boy` genişliğinde kimlik aralıklarıyla (`$1`, `$2`) sırayla çalışır; her parça kendi işleminde. #53 gibi
  393 bin satırlık tabloyu tek UPDATE'le doldurmak Supabase'de zaman aşımına uğrayıp tümüyle geri alınmıştı.
- **Geçersiz index kurtarma:** `CREATE INDEX CONCURRENTLY` yarıda kesilirse PostgreSQL `INVALID` bir index bırakır ve
  `IF NOT EXISTS` onu "var" sanıp atlar — index sessizce hiç işe yaramaz. Çalıştırıcı, ifadeyi çalıştırmadan önce aynı
  adlı geçersiz index'i `DROP INDEX CONCURRENTLY` ile temizler.
- Kayıt tablosu olmayan eski bir kurulumda bütün dosyalar sırayla uygulanıyor; migration'lar
  `IF NOT EXISTS` ile yazılı, yükseltme testi (`test_onprem_package_live.py`) bunu gerçek veriyle sınıyor.

Supabase (bulut) bu çalıştırıcıyı varsayılan olarak KULLANMIYOR: orada migration'lar DEPLOY.md sırasıyla uygulanıyor.
Uzun süren tek bir migration'ı (#53 gibi) parçalı uygulamak için:

    python -m app.migrations_runner <migration dizini> --only <dosya adı> --no-record

`--no-record`: kayıt tablosuna dokunmaz (bulut veritabanına `dbace_meta` şeması eklemez). DATABASE_URL doğrudan bağlantı
olmalı (havuzlayıcı değil).

    python -m app.migrations_runner <migration dizini>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.migration_sql import Statement, split_statements

logger = logging.getLogger(__name__)

META_SCHEMA = "dbace_meta"
META_TABLE = f"{META_SCHEMA}.applied_migrations"

_CONCURRENT_INDEX = re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+(?:IF\s+NOT\s+EXISTS\s+)?("?[\w.]+"?)', re.IGNORECASE)
_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_STATEMENT_TIMEOUT = re.compile(r"^\d+(?:ms|s|min)?$")

#: Parçalı ifade ilerleme günlüğü: bu kadar parçada bir.
PROGRESS_EVERY_CHUNKS = 10


@dataclass
class StatementStat:
    """Ölçüm kaydı: hangi dosyanın hangi ifadesi/parçası ne kadar sürdü, kaç satıra dokundu."""

    file: str
    line: int
    kind: str  # "statement" | "chunk"
    seconds: float
    rows: int = 0
    text: str = ""


def migration_files(directory: Path) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise SystemExit(f"migration bulunamadı: {directory}")
    return files


def _statements(sql: str) -> list[str]:
    """Geriye dönük uyumluluk: yalnızca ifade metinleri."""
    return [st.sql for st in split_statements(sql)]


def needs_autocommit(statements: list[Statement]) -> bool:
    """İşlem bloğunda çalışamayan dosya: CONCURRENTLY ya da parçalı ifade içeriyor."""
    return any(st.chunked or "CONCURRENTLY" in st.upper for st in statements)


def _rows(status: str) -> int:
    match = re.search(r"(\d+)\s*$", status or "")
    return int(match.group(1)) if match else 0


async def _drop_invalid_index(conn, quoted_name: str) -> bool:
    """Yarıda kesilmiş CREATE INDEX CONCURRENTLY'nin bıraktığı geçersiz index'i temizler."""
    name = quoted_name.strip('"').split(".")[-1]
    row = await conn.fetchrow(
        "SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "WHERE c.relname = $1 AND NOT i.indisvalid "
        "AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = current_schema())",
        name,
    )
    if row is None:
        return False
    logger.warning("geçersiz index bulundu (yarıda kesilmiş CONCURRENTLY): %s — silinip yeniden kurulacak", name)
    await conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
    return True


async def _run_chunked(conn, statement: Statement, file: str, stats: list[StatementStat] | None) -> int:
    table, size = statement.chunk
    if not _TABLE_NAME.match(table):
        raise ValueError(f"{file}:{statement.line}: geçersiz tablo adı {table!r}")
    bounds = await conn.fetchrow(f'SELECT min(id) AS lo, max(id) AS hi FROM "{table}"')
    if bounds is None or bounds["lo"] is None:
        logger.info("%s:%s parçalı ifade: %s boş, atlandı", file, statement.line, table)
        return 0
    lo, hi = int(bounds["lo"]), int(bounds["hi"])
    total_chunks = (hi - lo) // size + 1
    total_rows = 0
    for index, start in enumerate(range(lo, hi + 1, size), 1):
        started = time.monotonic()
        rows = _rows(await conn.execute(statement.sql, start, start + size))
        elapsed = time.monotonic() - started
        total_rows += rows
        if stats is not None:
            stats.append(StatementStat(file, statement.line, "chunk", elapsed, rows, statement.sql[:80]))
        if index % PROGRESS_EVERY_CHUNKS == 0 or index == total_chunks:
            logger.info("%s:%s %s parça %s/%s, %s satır güncellendi", file, statement.line, table, index, total_chunks,
                        total_rows)
    return total_rows


async def _run_file_without_transaction(conn, path: Path, statements: list[Statement],
                                        stats: list[StatementStat] | None) -> None:
    for statement in statements:
        started = time.monotonic()
        rows = 0
        if statement.chunked:
            rows = await _run_chunked(conn, statement, path.name, stats)
        else:
            index = _CONCURRENT_INDEX.search(statement.sql)
            if index:
                await _drop_invalid_index(conn, index.group(1))
            rows = _rows(await conn.execute(statement.sql))
        if stats is not None and not statement.chunked:
            stats.append(StatementStat(path.name, statement.line, "statement", time.monotonic() - started, rows,
                                       statement.sql[:80]))


async def apply_migrations(
    conn,
    directory: Path,
    *,
    until: str | None = None,
    only: str | None = None,
    record: bool = True,
    statement_timeout: str | None = None,
    stats: list[StatementStat] | None = None,
) -> list[str]:
    """Uygulanmamış migration'ları uygular; uygulananların adlarını döner.

    `until`: bu addan ÖNCEKİ dosyalar (testte kademeli kurulum). `only`: yalnızca bu dosya. `record=False`: kayıt
    tablosuna dokunma (dosya her seferinde çalışır). `statement_timeout`: bağlantıya ifade süre sınırı ("8s") — ölçek
    testinde yönetilen veritabanının sınırını taklit eder. `stats`: ifade/parça ölçümleri buraya eklenir."""
    if statement_timeout is not None:
        if not _STATEMENT_TIMEOUT.match(statement_timeout):
            raise ValueError(f"geçersiz statement_timeout: {statement_timeout!r}")
        await conn.execute(f"SET statement_timeout = '{statement_timeout}'")
    done: set[str] = set()
    if record:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {META_SCHEMA}")
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {META_TABLE} (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {row["version"] for row in await conn.fetch(f"SELECT version FROM {META_TABLE}")}
    applied: list[str] = []
    for path in migration_files(directory):
        if only is not None and path.name != only:
            continue
        if until is not None and path.name >= until:
            continue
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        logger.info("migration uygulanıyor: %s", path.name)
        statements = split_statements(sql)
        started = time.monotonic()
        if needs_autocommit(statements):
            await _run_file_without_transaction(conn, path, statements, stats)
            if record:
                await conn.execute(f"INSERT INTO {META_TABLE} (version) VALUES ($1)", path.name)
        else:
            async with conn.transaction():
                await conn.execute(sql)
                if record:
                    await conn.execute(f"INSERT INTO {META_TABLE} (version) VALUES ($1)", path.name)
            if stats is not None:
                stats.append(StatementStat(path.name, 1, "statement", time.monotonic() - started, 0, "(işlem)"))
        applied.append(path.name)
    if only is not None and not applied and not (directory / only).exists():
        raise SystemExit(f"migration dosyası yok: {directory / only}")
    return applied


def _asyncpg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main(directory: Path, *, only: str | None = None, record: bool = True,
               statement_timeout: str | None = None) -> int:
    import asyncpg

    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("DATABASE_URL PostgreSQL değil; migration çalıştırıcısı atlandı (SQLite: migrate_schema).")
        return 0
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        applied = await apply_migrations(conn, directory, only=only, record=record,
                                         statement_timeout=statement_timeout
                                         or os.environ.get("DBACE_MIGRATION_STATEMENT_TIMEOUT") or None)
    finally:
        await conn.close()
    total = len(migration_files(directory))
    print(f"migration: {len(applied)} yeni uygulandı, toplam {total} ({', '.join(applied) if applied else 'değişiklik yok'})")
    return 0


def parse_args_for_test(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="dbace migration çalıştırıcısı")
    parser.add_argument("directory", nargs="?", default="/app/migrations")
    parser.add_argument("--only", help="yalnızca bu dosyayı uygula (ad)")
    parser.add_argument("--no-record", action="store_true", help="kayıt tablosuna dokunma; dosya her seferinde çalışır")
    parser.add_argument("--statement-timeout", help="bağlantıya ifade süre sınırı, ör. 8s")
    return parser.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = parse_args_for_test(sys.argv[1:])
    raise SystemExit(asyncio.run(main(Path(args.directory), only=args.only, record=not args.no_record,
                                      statement_timeout=args.statement_timeout)))
