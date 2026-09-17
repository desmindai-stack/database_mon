"""Canlı PostgreSQL testlerinin ORTAK kurulumu (Faz 31 Commit 4).

Roller, şemalar, tablolar, fonksiyonlar ve test verisi burada, idempotent. Faz 31 Commit 4'e
kadar bu kurulum dört test dosyasına dağılmıştı ve biri (`test_explain_live_postgres.py`)
yalnızca tablo ZATEN VARSA çalışıyordu: veri ekleyen SQL'de `g %% 3` yazıyordu — PostgreSQL'de
böyle bir operatör yok. Elle kurulmuş, tabloları hazır konteynerde o dal hiç koşmadığı için
fark edilmedi; temiz konteynerde kırılırdı.

Konteynerlerin kendisi: `scripts/live_pg.py up`.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse, urlunparse

import pytest

from app.collectors.base import ConnectionTarget

LIVE_DSNS = [d.strip() for d in os.environ.get("DBACE_TEST_PG_DSN", "").split(",") if d.strip()]
SKIP_REASON = (
    "Gerçek PostgreSQL yok. `python scripts/live_pg.py up` ile konteynerleri kurup yazdırdığı "
    "DBACE_TEST_PG_DSN değerini tanımlayın."
)

#: DSN tanımlıyken canlı testte İZİN VERİLEN tek atlama: sunucu sürümü özelliği desteklemiyor.
#: Atlama gerekçesi sunucunun GERÇEK sürüm numarasını ve gereken asgari sürümü taşıyor;
#: `conftest.py` denetimi ikisini yeniden karşılaştırıyor — elle yazılmış bir istisna listesi yok.
VERSION_SKIP_PATTERN = re.compile(r"sürüm koşulu: sunucu (\d+) < (\d+)")


def skip_below_version(server_version_num: int, minimum: int, feature: str) -> None:
    if int(server_version_num) < int(minimum):
        pytest.skip(f"sürüm koşulu: sunucu {int(server_version_num)} < {int(minimum)} — {feature}")


def disallowed_live_skip(reason: str) -> bool:
    """Canlı testin atlanma gerekçesi bir sürüm koşulu DEĞİLSE True."""
    match = VERSION_SKIP_PATTERN.search(reason or "")
    return not (match and int(match.group(1)) < int(match.group(2)))


ROLE_PASSWORD = "dbace_it_pw"
#: rol anahtarı → (rol adı, CREATE ROLE öznitelikleri)
ROLES = {
    "super": ("dbace_it_super", "SUPERUSER"),
    "monitor": ("dbace_it_monitor", "IN ROLE pg_monitor"),
    # dbace DIŞI bir uygulama rolü: imza filtresinin ters yönünü (farklı userid) ölçmek için.
    "app": ("dbace_it_app", ""),
}

#: Test verisinin kurulduğu, hypopg'SUZ ikinci veritabanı (bkz. scripts/live_pg.py).
NO_HYPOPG_DATABASE = "dbace_nohypopg"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (id bigserial PRIMARY KEY, name text NOT NULL, segment text NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id bigserial PRIMARY KEY, customer_id bigint NOT NULL, status text NOT NULL,
    total numeric NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS adv_users (id bigserial PRIMARY KEY, email text NOT NULL, name text NOT NULL, note text);
CREATE TABLE IF NOT EXISTS adv_events (id bigserial PRIMARY KEY, kind text NOT NULL, happened_at timestamp NOT NULL);
CREATE SCHEMA IF NOT EXISTS adv_other;
CREATE SCHEMA IF NOT EXISTS adv_third;
CREATE TABLE IF NOT EXISTS adv_other.adv_invoices (id bigserial PRIMARY KEY, state text NOT NULL);
DROP TABLE IF EXISTS public.adv_ledger;
CREATE TABLE IF NOT EXISTS adv_other.adv_ledger (id bigserial PRIMARY KEY, amount numeric);
CREATE TABLE IF NOT EXISTS adv_third.adv_ledger (id bigserial PRIMARY KEY, amount numeric);
CREATE TABLE IF NOT EXISTS marker_probe (id int PRIMARY KEY, note text);
CREATE TABLE IF NOT EXISTS ps_write_probe (id bigserial PRIMARY KEY, note text);
CREATE OR REPLACE FUNCTION ps_writer() RETURNS int LANGUAGE sql AS
    $$ INSERT INTO ps_write_probe (note) VALUES ('yazıldı') RETURNING 1 $$;
-- İç içe (toplevel=false) çalıştırma üretmek için: PL/pgSQL gövdesindeki sorgu üst düzey
-- sorguyla aynı şekilde. pg_sleep: toplayıcı ortalama süreye göre İLK 20'yi okuyor; hızlı bir
-- test sorgusu taban döngüsünde listeye girmeyip fark yerine kümülatif sayılırdı.
CREATE OR REPLACE FUNCTION adv_nested_count() RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM orders WHERE status = 'paid' AND (SELECT pg_sleep(0.03)) IS NOT NULL;
    RETURN n;
END $$;
GRANT USAGE ON SCHEMA public TO dbace_it_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbace_it_app;
"""

_DATA = (
    ("customers", "INSERT INTO customers (name, segment) SELECT 'c'||g, CASE WHEN g % 3 = 0 THEN 'gold' ELSE 'std' END "
                  "FROM generate_series(1, 2000) g"),
    ("orders", "INSERT INTO orders (customer_id, status, total, created_at) SELECT (g % 2000) + 1, "
               "CASE WHEN g % 5 = 0 THEN 'paid' ELSE 'new' END, (g % 900)::numeric, now() - (g || ' minutes')::interval "
               "FROM generate_series(1, 50000) g"),
    ("adv_users", "INSERT INTO adv_users (email, name) SELECT 'User'||g||'@Example.com', 'n'||g FROM generate_series(1, 30000) g"),
    ("adv_events", "INSERT INTO adv_events (kind, happened_at) SELECT 'k'||(g % 7), timestamp '2026-01-01' + (g || ' minutes')::interval "
                   "FROM generate_series(1, 30000) g"),
    ("adv_other.adv_invoices", "INSERT INTO adv_other.adv_invoices (state) SELECT 's'||(g % 11) FROM generate_series(1, 5000) g"),
    ("marker_probe", "INSERT INTO marker_probe SELECT g, 'n'||g FROM generate_series(1, 100) g"),
)


async def ensure_roles(conn) -> None:
    """Küme düzeyindeki roller — idempotent."""
    for role_name, attrs in ROLES.values():
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role_name):
            await conn.execute(f"CREATE ROLE {role_name} LOGIN PASSWORD '{ROLE_PASSWORD}' {attrs}")


async def prepare_live_database(conn) -> None:
    """Bağlı olunan veritabanına test şemasını ve verisini kurar — idempotent."""
    await ensure_roles(conn)
    await conn.execute(_SCHEMA_SQL)
    for table, insert in _DATA:
        if not await conn.fetchval(f"SELECT count(*) FROM {table}"):
            await conn.execute(insert)
    await conn.execute("ANALYZE")
    # Önceki koşudan kalmış olabilecek doğrulama index'leri (ifade index'i testleri gerçek DDL çalıştırıyor).
    for row in await conn.fetch("SELECT schemaname, indexname FROM pg_indexes WHERE indexname LIKE 'idx_dbace_%'"):
        await conn.execute(f'DROP INDEX IF EXISTS {row["schemaname"]}."{row["indexname"]}"')


def with_database(dsn: str, database: str) -> str:
    return urlunparse(urlparse(dsn)._replace(path=f"/{database}"))


def target_for(dsn: str, role: str = "super", database: str | None = None) -> ConnectionTarget:
    url = urlparse(dsn)
    return ConnectionTarget(
        host=url.hostname or "127.0.0.1",
        port=url.port or 5432,
        database=database or (url.path or "/postgres").lstrip("/"),
        username=ROLES[role][0],
        password=ROLE_PASSWORD,
    )


def dsn_id(dsn: str) -> str:
    return dsn.rsplit("@", 1)[-1]
