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
- **CONCURRENTLY sınırsız çalışır** (Faz 31 Commit 10f): oturuma set'lenmiş `statement_timeout` (yönetilen
  veritabanının sınırı) yalnızca bu tek ifade için `0`'a çekilir, sonra geri yazılır — index parçalanamıyor ve
  tabloyu kilitlemiyor, ama bölünemediği için tek bir sınır aşımı bütün ifadeyi iptal ederdi. Diğer (chunked/düz)
  ifadeler sınırı aynen korur.
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

#: Parçalı ifade ilerleme log'unun BİÇİMİ — `_run_chunked`'ın `logger.info` çağrısı VE onu okuyan testler
#: (`test_migrations_runner.py`, `test_onprem_package_live.py`) AYNI bu sabitten besleniyor (Faz 31 Commit 10g):
#: biri log metnini değiştirip diğerini güncellemeyi unutursa sessizce ayrışmasınlar diye tek kaynak.
CHUNK_PROGRESS_LOG_FORMAT = "%s:%s %s parça %s (id %s/%s, genişlik %s, %s yeniden deneme), %s satır güncellendi"
CHUNK_PROGRESS_LOG_RE = re.compile(
    r"(?P<file>[\w.]+):(?P<line>\d+) (?P<table>\w+) parça (?P<chunk_no>\d+) "
    r"\(id (?P<progress>\d+)/(?P<hi>\d+), genişlik (?P<width>\d+), (?P<retries>\d+) yeniden deneme\), "
    r"(?P<rows>\d+) satır güncellendi"
)
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


#: Parça süresi ifade sınırının en çok 1/CHUNK_BUDGET_DIVISOR'u olsun (Faz 31 Commit 10d madde A3). Bir parça bunu aşarsa
#: sunucu iptal eder, parça GERİ ALINIR (kendi işlemi) ve YARI genişlikle yeniden denenir.
CHUNK_BUDGET_DIVISOR = 3
#: Bu genişliğin altına inilmez: en dar parça bile bütçeyi aşıyorsa sorun parça boyu değil, sunucu — hata verilir.
MIN_CHUNK_SIZE = 250
#: İptalden sonra sunucuya (checkpoint/fsync birikimine) nefes aldırmak için bekleme.
CHUNK_RETRY_PAUSE_SECONDS = 1.0

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|min|h|d)?\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


def _timeout_seconds(setting: str | None) -> float | None:
    """`SHOW statement_timeout` çıktısı ('8s', '2667ms', '0') → saniye; sınır yoksa/bilinmiyorsa None."""
    match = _DURATION.match(setting or "")
    if not match:
        return None
    seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2) or "ms"]
    return seconds or None


async def _run_chunked(conn, statement: Statement, file: str, stats: list[StatementStat] | None,
                       chunk_budget_seconds: float | None = None) -> int:
    """Parçalı ifade: `[lo, hi]` kimlik aralığını `size` genişliğinde parçalarla çalıştırır.

    `size` bir ÜST SINIR: her parça `chunk_budget_seconds` (verilmezse oturumun `statement_timeout`unun 1/3'ü) ile
    sınırlanıyor. Bütçeyi aşan parça iptal edilip GERİ ALINIR ve yarı genişlikle yeniden denenir — ifadeler
    idempotent (`... IS NULL`) ve her parça kendi işleminde olduğu için sonuç değişmez, yalnızca tek seferlik bir
    duraksama (checkpoint/fsync birikimi; gerçek CI diskinde 4–5 sn'lik tek parçalık sıçramalar ölçüldü) tüm işi
    öldürmez ve hiçbir parça bütçenin üstünde BAŞARIYLA tamamlanmaz. Genişlik yalnızca küçülür (temkinli).
    """
    from asyncpg.exceptions import QueryCanceledError

    table, size = statement.chunk
    if not _TABLE_NAME.match(table):
        raise ValueError(f"{file}:{statement.line}: geçersiz tablo adı {table!r}")
    bounds = await conn.fetchrow(f'SELECT min(id) AS lo, max(id) AS hi FROM "{table}"')
    if bounds is None or bounds["lo"] is None:
        logger.info("%s:%s parçalı ifade: %s boş, atlandı", file, statement.line, table)
        return 0
    lo, hi = int(bounds["lo"]), int(bounds["hi"])

    current = await conn.fetchrow("SELECT current_setting('statement_timeout') AS v")
    current_setting = current["v"] if current is not None else None
    limit = _timeout_seconds(current_setting)
    budget = chunk_budget_seconds if chunk_budget_seconds is not None else (
        limit / CHUNK_BUDGET_DIVISOR if limit else None)
    if budget is not None:
        await conn.execute(f"SET statement_timeout = '{max(int(budget * 1000), 1)}ms'")

    total_rows, chunks_done, retries = 0, 0, 0
    width, position = size, lo
    try:
        while position <= hi:
            started = time.monotonic()
            try:
                rows = _rows(await conn.execute(statement.sql, position, position + width))
            except QueryCanceledError:
                if width <= MIN_CHUNK_SIZE:
                    raise
                retries += 1
                logger.warning("%s:%s %s parça [%s, %s) bütçeyi (%s sn) aştı, iptal edildi; %s → %s genişlikle yeniden denenecek",
                               file, statement.line, table, position, position + width,
                               "?" if budget is None else round(budget, 2), width, max(width // 2, MIN_CHUNK_SIZE))
                if stats is not None:
                    stats.append(StatementStat(file, statement.line, "retry", time.monotonic() - started, 0, statement.sql[:80]))
                width = max(width // 2, MIN_CHUNK_SIZE)
                await asyncio.sleep(CHUNK_RETRY_PAUSE_SECONDS)
                continue
            elapsed = time.monotonic() - started
            total_rows += rows
            chunks_done += 1
            if stats is not None:
                stats.append(StatementStat(file, statement.line, "chunk", elapsed, rows, statement.sql[:80]))
            position += width
            if chunks_done % PROGRESS_EVERY_CHUNKS == 0 or position > hi:
                logger.info(CHUNK_PROGRESS_LOG_FORMAT, file, statement.line, table, chunks_done,
                            min(position - 1, hi), hi, width, retries, total_rows)
    finally:
        if budget is not None:
            restore = current_setting if current_setting not in (None, "") else "0"
            await conn.execute(f"SET statement_timeout = '{restore}'")
    return total_rows


async def _run_unbounded(conn, run) -> int:
    """`run` çalışırken oturumun `statement_timeout`unu geçici olarak kaldırır (Faz 31 Commit 10f).

    `CREATE INDEX CONCURRENTLY` parçalanamaz ve tabloyu kilitlemez — ama önceden `-- dbace:chunked`
    bir UPDATE'in ardından geldiğinde, oturumun yönetilen-veritabanı sınırını (Supabase 8 sn) taklit
    eden `statement_timeout` HÂLÂ set'liydi ve tek, bölünemeyen bu ifadeye de uygulanıyordu — 420 bin
    satırda index kurulumu bu sınırı aşıp `QueryCanceledError` ile iptal ediliyordu (#53'ün İLK sürümü
    bu sınıf hatayla değil ama YENİ sürümün CONCURRENTLY adımı da aynı sınıfa girdi — canlıda #53'ü elle
    kurarken zaten sınırı yükseltmiştik, aynı ihtiyaç). Diğer ifadeler (chunked ya da düz) sınırı korur.
    """
    current = await conn.fetchrow("SELECT current_setting('statement_timeout') AS v")
    original = current["v"] if current is not None else None
    restore = original if original not in (None, "") else "0"
    await conn.execute("SET statement_timeout = '0'")
    try:
        return await run()
    finally:
        await conn.execute(f"SET statement_timeout = '{restore}'")


async def _run_file_without_transaction(conn, path: Path, statements: list[Statement],
                                        stats: list[StatementStat] | None,
                                        chunk_budget_seconds: float | None = None) -> None:
    for statement in statements:
        started = time.monotonic()
        rows = 0
        if statement.chunked:
            rows = await _run_chunked(conn, statement, path.name, stats, chunk_budget_seconds)
        else:
            index = _CONCURRENT_INDEX.search(statement.sql)
            if index:
                async def _build(statement=statement, index=index) -> int:
                    await _drop_invalid_index(conn, index.group(1))
                    return _rows(await conn.execute(statement.sql))
                rows = await _run_unbounded(conn, _build)
            else:
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
    chunk_budget_seconds: float | None = None,
) -> list[str]:
    """Uygulanmamış migration'ları uygular; uygulananların adlarını döner.

    `until`: bu addan ÖNCEKİ dosyalar (testte kademeli kurulum). `only`: yalnızca bu dosya. `record=False`: kayıt
    tablosuna dokunma (dosya her seferinde çalışır). `statement_timeout`: bağlantıya ifade süre sınırı ("8s") — ölçek
    testinde yönetilen veritabanının sınırını taklit eder. `stats`: ifade/parça ölçümleri buraya eklenir. `chunk_budget_seconds`: parça başına süre bütçesi (varsayılan:
    oturumun `statement_timeout`unun 1/3'ü; bkz. `_run_chunked`)."""
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
            await _run_file_without_transaction(conn, path, statements, stats, chunk_budget_seconds)
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
