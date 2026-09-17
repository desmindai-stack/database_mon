"""`supabase/migrations/*.sql` dosyalarını ad sırasıyla, bir kez uygular (Faz 31 Commit 8).

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
- `CREATE INDEX CONCURRENTLY` içeren dosya işlem bloğunda çalışmaz: komutları tek tek, işlemsiz.
- Kayıt tablosu olmayan eski bir kurulumda bütün dosyalar sırayla uygulanıyor; migration'lar
  `IF NOT EXISTS` ile yazılı, yükseltme testi (`test_onprem_package_live.py`) bunu gerçek veriyle sınıyor.

Supabase (bulut) bu çalıştırıcıyı KULLANMIYOR: orada migration'lar DEPLOY.md sırasıyla elle uygulanıyor.

    python -m app.migrations_runner <migration dizini>
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

META_SCHEMA = "dbace_meta"
META_TABLE = f"{META_SCHEMA}.applied_migrations"


def migration_files(directory: Path) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise SystemExit(f"migration bulunamadı: {directory}")
    return files


def _statements(sql: str) -> list[str]:
    body = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in body.split(";") if s.strip()]


async def apply_migrations(conn, directory: Path) -> list[str]:
    """Uygulanmamış migration'ları uygular; uygulananların adlarını döner."""
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {META_SCHEMA}")
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    done = {row["version"] for row in await conn.fetch(f"SELECT version FROM {META_TABLE}")}
    applied: list[str] = []
    for path in migration_files(directory):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        logger.info("migration uygulanıyor: %s", path.name)
        # Yorumlar ayıklanınca bakılıyor: başka migration'lar CONCURRENTLY'den yorumda söz ediyor ve DO $$ gövdeleri
        # `;` ile bölünürse bozulurdu.
        if "CONCURRENTLY" in re.sub(r"--[^\n]*", "", sql).upper():
            for statement in _statements(sql):
                await conn.execute(statement)
            await conn.execute(f"INSERT INTO {META_TABLE} (version) VALUES ($1)", path.name)
        else:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(f"INSERT INTO {META_TABLE} (version) VALUES ($1)", path.name)
        applied.append(path.name)
    return applied


def _asyncpg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main(directory: Path) -> int:
    import asyncpg

    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("DATABASE_URL PostgreSQL değil; migration çalıştırıcısı atlandı (SQLite: migrate_schema).")
        return 0
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        applied = await apply_migrations(conn, directory)
    finally:
        await conn.close()
    total = len(migration_files(directory))
    print(f"migration: {len(applied)} yeni uygulandı, toplam {total} ({', '.join(applied) if applied else 'değişiklik yok'})")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/app/migrations"))))
