"""Şema eşliği: SQLite (`init_db` → `create_all` + `migrate_schema`) ile `supabase/migrations`
tablo ve kolon düzeyinde AYNI mı (Faz 31 Commit 5).

İki şema da çalışma anında ÇIKARILIYOR — elle tablo/kolon listesi yok:

- **PostgreSQL**: boş bir veritabanına `supabase/migrations/*.sql` ad sırasıyla uygulanıyor,
  `information_schema.columns` okunuyor.
- **SQLite, sıfırdan kurulum**: boş dosyada `init_db()`.
- **SQLite, yükseltme**: dosya, PostgreSQL'e yalnızca İLK migration uygulandığındaki tablo ve
  kolonlarla kuruluyor; sonra `init_db()`. `create_all` var olan tabloya kolon EKLEMEZ — sonradan
  eklenen her kolonu `migrate_schema` eklemek zorunda. Bu yol onu sınıyor.

SQLite tarafı AYRI SÜREÇTE (`DATABASE_URL` o dosyaya) koşuyor: uygulamanın motoru süreç başına tek.

On-prem'in şema yolu (Faz 31 Commit 8): konteyner açılışında `deploy/onprem/entrypoint.sh`
`python -m app.migrations_runner /app/migrations` çalıştırıyor, ardından uygulama `create_all`. Test bu
yolu entrypoint'ten OKUYARAK doğruluyor ve aynı çalıştırıcıyla kuruyor. Commit 5'te buradaki strict xfail
eski yolu (initdb'ye bağlı ilk dört migration + `create_all`) ölçüyordu: 60 kolon eksikti.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.live_pg import LIVE_DSNS, SKIP_REASON, dsn_id, with_database

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS = BACKEND.parent / "supabase" / "migrations"
ONPREM_COMPOSE = BACKEND.parent / "deploy" / "onprem" / "docker-compose.yml"
DATABASE = "dbace_schema_it"


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(scope="module", params=LIVE_DSNS[:1], ids=dsn_id)
def dsn(request):
    # Şema eşliği sunucu sürümüne bağlı değil; tek sürüm yeterli (hangisi olduğu çıktıda).
    return request.param


async def _fresh(dsn: str):
    admin = await asyncpg.connect(with_database(dsn, "postgres"))
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {DATABASE}")
    finally:
        await admin.close()
    return await asyncpg.connect(with_database(dsn, DATABASE))


async def apply_migration(conn, path: Path) -> None:
    """Migration'ı UYGULAYICIYLA çalıştır (Faz 31 Commit 10b): CONCURRENTLY/parçalı dosyalar işlem dışı, dolar-tırnaklı
    gövdeler bölünmeden — eskiden burada `;` ile bölen ayrı bir kopya vardı ve DO bloklarını parçalıyordu."""
    from app.migrations_runner import apply_migrations

    await apply_migrations(conn, path.parent, only=path.name, record=False)


async def pg_schema(conn) -> dict[str, set[str]]:
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
    )
    schema: dict[str, set[str]] = {}
    for row in rows:
        schema.setdefault(row["table_name"], set()).add(row["column_name"])
    return schema


async def migrations_schema(dsn: str, files: list[Path]) -> dict[str, set[str]]:
    conn = await _fresh(dsn)
    try:
        for path in files:
            await apply_migration(conn, path)
        return await pg_schema(conn)
    finally:
        await conn.close()


_SQLITE_SCRIPT = """
import asyncio, json, sqlite3, sys
from sqlalchemy import text
seed = json.loads(sys.argv[2])
path = sys.argv[1]
if seed:
    con = sqlite3.connect(path)
    for table, columns in seed.items():
        con.execute(f'CREATE TABLE "{table}" (' + ", ".join(f'"{c}" TEXT' for c in sorted(columns)) + ")")
    con.commit(); con.close()
from app import database
async def main():
    if sys.argv[3] == "create_all_only":
        from app import models  # noqa: F401
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
    else:
        await database.init_db()
    await database.engine.dispose()
asyncio.run(main())
con = sqlite3.connect(path)
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
print(json.dumps({t: [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')] for t in tables}))
"""


def sqlite_schema(seed: dict[str, set[str]] | None, mode: str = "init_db") -> dict[str, set[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schema.db"
        env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path.as_posix()}"}
        out = subprocess.run(
            [sys.executable, "-c", _SQLITE_SCRIPT, str(path), json.dumps({k: sorted(v) for k, v in (seed or {}).items()}), mode],
            cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        assert out.returncode == 0, out.stderr[-3000:]
        return {t: set(cols) for t, cols in json.loads(out.stdout.strip().splitlines()[-1]).items()}


def schema_diff(left: dict[str, set[str]], right: dict[str, set[str]]) -> list[str]:
    diff = [f"yalnız solda tablo: {t}" for t in sorted(set(left) - set(right))]
    diff += [f"yalnız sağda tablo: {t}" for t in sorted(set(right) - set(left))]
    for table in sorted(set(left) & set(right)):
        diff += [f"yalnız solda kolon: {table}.{c}" for c in sorted(left[table] - right[table])]
        diff += [f"yalnız sağda kolon: {table}.{c}" for c in sorted(right[table] - left[table])]
    return diff


@pytest.fixture(scope="module")
def migration_files():
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert len(files) > 1
    return files


@pytest.fixture(scope="module")
def full_pg(dsn, migration_files):
    return asyncio.run(migrations_schema(dsn, migration_files))


def test_sqlite_fresh_install_matches_supabase_migrations(full_pg):
    sqlite = sqlite_schema(None)
    diff = schema_diff(sqlite, full_pg)
    log("SQLite sıfırdan ↔ migration'lar", f"{len(full_pg)} tablo, {sum(map(len, full_pg.values()))} kolon; fark: {diff or 'yok'}")
    assert diff == []


def test_sqlite_upgrade_through_migrate_schema_matches_supabase_migrations(dsn, migration_files, full_pg):
    baseline = asyncio.run(migrations_schema(dsn, migration_files[:1]))
    upgraded = sqlite_schema(baseline)
    diff = schema_diff(upgraded, full_pg)
    added = sum(len(full_pg[t] - baseline.get(t, set())) for t in full_pg if t in baseline)
    log("SQLite yükseltme ↔ migration'lar",
        f"taban {migration_files[0].name}: {len(baseline)} tablo; migrate_schema'nın eklemesi gereken {added} kolon; fark: {diff or 'yok'}")
    assert diff == []


def test_negative_control_upgrade_without_migrate_schema_is_detected(dsn, migration_files, full_pg):
    """`migrate_schema` çalışmasaydı (yalnızca create_all) test farkı görüyor."""
    baseline = asyncio.run(migrations_schema(dsn, migration_files[:1]))
    diff = schema_diff(sqlite_schema(baseline, mode="create_all_only"), full_pg)
    log("negatif kontrol: migrate_schema'sız", f"{len(diff)} fark, ör. {diff[:3]}")
    assert diff


def test_negative_control_a_missing_migration_is_detected(dsn, migration_files, full_pg):
    """Bir ADD COLUMN migration'ı atlanırsa PostgreSQL tarafı eksik kalıyor ve test farkı görüyor."""
    skipped = next(p for p in reversed(migration_files) if "ADD COLUMN" in p.read_text(encoding="utf-8").upper())
    partial = asyncio.run(migrations_schema(dsn, [p for p in migration_files if p != skipped]))
    diff = schema_diff(sqlite_schema(None), partial)
    log("negatif kontrol: atlanan migration", f"{skipped.name} → {diff}")
    assert diff


ONPREM_ENTRYPOINT = BACKEND.parent / "deploy" / "onprem" / "entrypoint.sh"
ONPREM_DOCKERFILE = BACKEND.parent / "deploy" / "onprem" / "Dockerfile.backend"


async def _onprem_install(dsn: str, directory: Path, *, rerun_without_registry: bool = False) -> tuple[dict, list, list]:
    """Konteyner açılışının aynısı: migration çalıştırıcısı, sonra uygulamanın `create_all`'u."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migrations_runner import META_SCHEMA, apply_migrations
    from app.models import Base

    conn = await _fresh(dsn)
    try:
        first = await apply_migrations(conn, directory)
        if rerun_without_registry:
            # Kayıt tablosu olmayan ESKİ kurulum: bütün dosyalar dolu şemaya yeniden uygulanıyor.
            await conn.execute(f"DROP SCHEMA {META_SCHEMA} CASCADE")
            first = await apply_migrations(conn, directory)
        second = await apply_migrations(conn, directory)
    finally:
        await conn.close()
    url = with_database(dsn, DATABASE).replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    async with engine.begin() as sa_conn:
        await sa_conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    conn = await asyncpg.connect(with_database(dsn, DATABASE))
    try:
        return await pg_schema(conn), first, second
    finally:
        await conn.close()


def test_onprem_install_path_is_the_migration_runner():
    """Paketin gerçekten bu yolu kullandığı dosyalardan: entrypoint çalıştırıcıyı çağırıyor, imaj migration'ları
    kopyalıyor, compose initdb'ye migration BAĞLAMIYOR (iki yol yarışmasın)."""
    entrypoint = ONPREM_ENTRYPOINT.read_text(encoding="utf-8")
    dockerfile = ONPREM_DOCKERFILE.read_text(encoding="utf-8")
    compose = ONPREM_COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^\s*python -m app\.migrations_runner /app/migrations\s*$", entrypoint, re.M)
    assert re.search(r"^COPY supabase/migrations \./migrations\s*$", dockerfile, re.M)
    assert "docker-entrypoint-initdb.d" not in compose


@pytest.mark.parametrize("rerun_without_registry", [False, True], ids=["yeni-kurulum", "kayitsiz-eski-kurulum"])
def test_onprem_install_path_matches_supabase_migrations(dsn, migration_files, full_pg, rerun_without_registry):
    schema, first, second = asyncio.run(_onprem_install(dsn, MIGRATIONS, rerun_without_registry=rerun_without_registry))
    diff = schema_diff(schema, full_pg)
    log("on-prem kurulum ↔ migration'lar",
        f"ilk tur {len(first)} dosya, ikinci tur {len(second)}; {len(schema)} tablo; fark: {diff or 'yok'}")
    assert len(first) == len(migration_files) and second == []
    assert diff == []


def test_negative_control_onprem_path_without_an_add_column_migration_is_detected(dsn, migration_files, full_pg, tmp_path):
    skipped = next(p for p in reversed(migration_files) if "ADD COLUMN" in p.read_text(encoding="utf-8").upper())
    for path in migration_files:
        if path != skipped:
            (tmp_path / path.name).write_bytes(path.read_bytes())
    schema, _, _ = asyncio.run(_onprem_install(dsn, tmp_path))
    diff = schema_diff(schema, full_pg)
    log("negatif kontrol: on-prem, atlanan migration", f"{skipped.name} → {diff}")
    assert diff
