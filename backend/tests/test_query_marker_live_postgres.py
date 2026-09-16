"""`/* dbace */` imzasının GERÇEK PostgreSQL'de doğrulanması (Faz 31 İŞ 1).

Sahte bağlantı burada hiçbir şey kanıtlamaz: soru "sarmalayıcı metni değiştirdi mi" değil,
"sunucunun pg_stat_statements'ında saklanan metin imzayı taşıyor mu". Bunu yalnızca gerçek
sunucu cevaplayabilir.

Çalıştırma (bkz. test_explain_live_postgres.py):

    set DBACE_TEST_PG_DSN=postgresql://postgres:dbace@127.0.0.1:55432/dbace,postgresql://postgres:dbace@127.0.0.1:55433/dbace
    pytest tests/test_query_marker_live_postgres.py -v -s

DSN'deki kullanıcı YALNIZCA kurulum ve doğrulama için kullanılıyor. dbace tarafı iki ayrı
rolle koşuyor, çünkü doğrulama bağlantısının kendi sorguları da pg_stat_statements'a düşüyor
ve onları dbace'in sorgularından ayırmanın tek güvenilir yolu `userid`:

* `dbace_it_super` — SUPERUSER (yetkili izleme kullanıcısı)
* `dbace_it_monitor` — yalnızca `pg_monitor` üyesi (en az yetkili gerçekçi izleme kullanıcısı)
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest

from app.collectors.base import ConnectionTarget
from app.collectors.query_marker import DBACE_QUERY_MARKER, connect_marked
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module
from app.services import wait_sampling
from app.services.credentials import encrypt_secret
from app.services.explain_service import PostgreSQLExplainService
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.prerequisites import check_postgresql_prerequisites

asyncpg = pytest.importorskip("asyncpg")

_DSNS = [d.strip() for d in os.environ.get("DBACE_TEST_PG_DSN", "").split(",") if d.strip()]

pytestmark = pytest.mark.skipif(
    not _DSNS, reason="Gerçek PostgreSQL yok. DBACE_TEST_PG_DSN tanımlayın."
)

ROLE_PASSWORD = "dbace_it_pw"

#: İstemciden İMZALANAMAYAN iç içe (toplevel=false) ifadeler. Bunları dbace göndermiyor:
#: bir eklenti, dbace'in (imzalı) çağrısı sırasında sunucu içinden SPI ile çalıştırıyor.
#: Liste açık tutuluyor — buraya kanıtsız ekleme yapılmamalı. Yalnızca
#: `pg_stat_statements.track = all` iken görünürler (varsayılan `top`).
EXTENSION_INTERNAL_STATEMENTS = {
    # PG 17.11'de doğrulandı: `/* dbace */ SELECT hypopg_create_index(...)` tek başına
    # çalıştırıldığında bu ifade toplevel=false olarak düşüyor.
    "SELECT max(oid) FROM pg_catalog.pg_class WHERE oid < $1": "hypopg_create_index iç sorgusu",
}
ROLES = {
    "super": ("dbace_it_super", "SUPERUSER"),
    "monitor": ("dbace_it_monitor", "IN ROLE pg_monitor"),
}


@pytest.fixture(params=_DSNS, ids=lambda d: d.rsplit("@", 1)[-1])
def dsn(request):
    return request.param


@pytest.fixture
async def admin(dsn):
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    for name, attrs in ROLES.values():
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
        if not exists:
            await conn.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{ROLE_PASSWORD}' {attrs}")
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS marker_probe (id int PRIMARY KEY, note text)"
    )
    await conn.execute(
        "INSERT INTO marker_probe SELECT g, 'n'||g FROM generate_series(1, 100) g "
        "ON CONFLICT DO NOTHING"
    )
    try:
        yield conn
    finally:
        await conn.close()


def _target(dsn: str, role: str) -> ConnectionTarget:
    url = urlparse(dsn)
    return ConnectionTarget(
        host=url.hostname or "127.0.0.1",
        port=url.port or 5432,
        database=(url.path or "/postgres").lstrip("/"),
        username=ROLES[role][0],
        password=ROLE_PASSWORD,
    )


async def _server_label(admin) -> str:
    return await admin.fetchval("SELECT current_setting('server_version')")


# --- 1.4: yorum queryid'ye giriyor mu? -----------------------------------------------------


@pytest.mark.parametrize("marked_first", [False, True], ids=["imzasiz-once", "imzali-once"])
async def test_comment_does_not_change_queryid_and_first_text_wins(admin, marked_first):
    """ÖLÇÜM: aynı sorgu önce imzasız sonra imzalı (ve tersi) çalıştırılıyor."""
    await admin.execute("SELECT pg_stat_statements_reset()")
    plain = "SELECT count(*) FROM marker_probe WHERE id = 42"
    marked = f"{DBACE_QUERY_MARKER} {plain}"
    for sql in ([marked, plain] if marked_first else [plain, marked]):
        await admin.fetchval(sql)

    rows = await admin.fetch(
        "SELECT queryid, calls, toplevel, query FROM pg_stat_statements "
        "WHERE query ILIKE '%marker_probe WHERE id%' AND query NOT ILIKE '%pg_stat_statements%'"
    )
    print(f"\n[{await _server_label(admin)}] marked_first={marked_first}")
    for r in rows:
        print(f"  queryid={r['queryid']} calls={r['calls']} toplevel={r['toplevel']} query={r['query']!r}")

    assert len(rows) == 1, "yorum farkı AYRI bir kayıt oluşturdu — tasarım varsayımı yanlış"
    assert rows[0]["calls"] == 2
    assert rows[0]["query"].startswith(DBACE_QUERY_MARKER) is marked_first


# --- 1.6: application_name pg_stat_statements'ta yok ----------------------------------------


async def test_application_name_is_not_a_pg_stat_statements_column(admin):
    columns = [
        r["attname"]
        for r in await admin.fetch(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'pg_stat_statements'::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
        )
    ]
    print(f"\n[{await _server_label(admin)}] pg_stat_statements: {len(columns)} sütun: {columns}")
    assert "application_name" not in columns
    assert "userid" in columns


@pytest.mark.parametrize("role", ["super", "monitor"])
async def test_connections_carry_application_name(admin, dsn, role):
    target = _target(dsn, role)
    conn = await connect_marked(
        host=target.host, port=target.port, database=target.database,
        user=target.username, password=target.password, statement_cache_size=0,
    )
    try:
        pid = await conn.fetchval("SELECT pg_backend_pid()")
        seen = await admin.fetchval(
            "SELECT application_name FROM pg_stat_activity WHERE pid = $1", pid
        )
    finally:
        await conn.close()
    print(f"\n[{await _server_label(admin)}] rol={target.username} pid={pid} application_name={seen!r}")
    assert seen == "dbace"


# --- 1.1: dbace'in gönderdiği HER sorgu imzalı ---------------------------------------------


async def _run_real_call_paths(dsn: str, role: str) -> dict[str, str]:
    """dbace'in PostgreSQL'e gittiği yolları GERÇEK giriş noktalarından çalıştırır.

    Dönen sözlük her yolun sonucu: "ok", ya da yetki/özellik eksikliğinde yakalanan hata.
    Hata burada bilgi — testin konusu imza, yolun başarısı değil; ama sonuç raporlanıyor.
    """
    target = _target(dsn, role)
    outcome: dict[str, str] = {}

    # (a) Toplama döngüsü — zamanlayıcının çağırdığı fonksiyonun ta kendisi.
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"marker-it-{role}-{uuid.uuid4().hex[:8]}", engine="postgresql",
            host=target.host, port=target.port, database=target.database,
            username=target.username, password=encrypt_secret(target.password),
        )
        session.add(instance)
        await session.flush()
        try:
            await collection_module.collect_instance(instance, session)
            outcome["collection.collect_instance"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["collection.collect_instance"] = f"HATA: {exc}"

        # (b) Bekleme örnekleyicisi — kalıcı bağlantı yolu.
        try:
            await wait_sampling._sample_instance(instance, datetime.now(UTC))
            outcome["wait_sampling._sample_instance"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["wait_sampling._sample_instance"] = f"HATA: {exc}"
        finally:
            sampler = wait_sampling._samplers.pop(instance.id, None) if hasattr(wait_sampling, "_samplers") else None
            if sampler is not None:
                await wait_sampling._drop_connection(sampler)
        await session.rollback()

    # (c) İsteğe bağlı yollar — API uçlarının çağırdığı servisler.
    for label, call in (
        ("prerequisites.check_postgresql_prerequisites", lambda: check_postgresql_prerequisites(target)),
        ("explain_service.explain", lambda: PostgreSQLExplainService(target).explain(
            "SELECT note FROM marker_probe WHERE id = $1")),
        ("index_advisor.advise", lambda: PostgreSQLIndexAdvisor(target).advise(
            "SELECT note FROM marker_probe WHERE note = $1", calls=100)),
    ):
        try:
            await call()
            outcome[label] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome[label] = f"HATA: {type(exc).__name__}: {str(exc)[:160]}"
    return outcome


@pytest.mark.parametrize("role", ["super", "monitor"])
async def test_every_statement_dbace_sends_is_marked(admin, dsn, role):
    role_name = ROLES[role][0]
    await admin.execute("SELECT pg_stat_statements_reset()")

    outcome = await _run_real_call_paths(dsn, role)

    rows = await admin.fetch(
        """
        SELECT s.toplevel, s.calls, s.query
        FROM pg_stat_statements s
        JOIN pg_roles r ON r.oid = s.userid
        WHERE r.rolname = $1
        ORDER BY s.toplevel DESC, s.query
        """,
        role_name,
    )
    top = [r for r in rows if r["toplevel"]]
    nested = [r for r in rows if not r["toplevel"]]
    unmarked_top = [r["query"] for r in top if not r["query"].lstrip().startswith(DBACE_QUERY_MARKER)]

    print(f"\n[{await _server_label(admin)}] rol={role_name}")
    for path, result in outcome.items():
        print(f"  yol {path}: {result}")
    print(f"  pg_stat_statements: {len(top)} üst düzey kayıt, {len(nested)} iç içe (toplevel=false) kayıt")
    print(f"  imzasız üst düzey kayıt: {len(unmarked_top)}")
    for q in unmarked_top:
        print(f"    İMZASIZ: {q[:160]!r}")
    for r in nested:
        print(f"    iç içe: {r['query'][:120]!r}")

    unexplained_nested = [
        r["query"]
        for r in nested
        if not r["query"].lstrip().startswith(DBACE_QUERY_MARKER)
        and r["query"] not in EXTENSION_INTERNAL_STATEMENTS
    ]

    assert top, "hiç kayıt yok — gerçek yollar çalışmadı, test bir şey kanıtlamıyor"
    assert not unmarked_top
    assert not unexplained_nested
