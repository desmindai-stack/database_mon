"""CANLI 502 DÜZELTMESİ — sık sorgulanan tabloların bileşik indeksleri.

**Belirti:** `GET /api/instances/{id}/insights` Railway'de 502 Bad Gateway. Yan etki olarak
tarayıcıda CORS hatası: 502 uygulamadan değil gateway'den geldiği için yanıtta CORS başlığı yok.

**Sebep:** uç canlı veritabanına hiç bağlanmıyor — tamamı dbace'in kendi veritabanına giden
sorgular. Kalıp her yerde aynı:

    SELECT ... FROM <tablo> WHERE instance_id = ? ORDER BY collected_at DESC LIMIT 1

Tablolarda yalnızca AYRI AYRI `instance_id` ve `collected_at` indeksleri vardı. İkisi de bu
soruyu ucuza cevaplayamaz: ya o instance'ın bütün satırları çekilip sıralanır, ya `collected_at`
indeksi sondan taranıp `instance_id` ile elenir — ve veri göndermeyi durdurmuş bir instance için
ikincisi tablonun tamamını taramaya dönüşür.

Hacim bunu ölümcül yapıyor: 15 saniyelik toplama aralığında `slow_query_samples`'a döngü başına
20 satır yazılıyor — instance başına ayda ~3.5 milyon satır.

Bu testler indeksin hem modelde hem migration'da hem de canlı şemada bulunduğunu doğruluyor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import engine, init_db
from app.models import MetricSample, SlowQuerySample

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "20260909090000_hot_table_composite_indexes.sql"

# (tablo, indeks adı) — sorgu kalıbı `WHERE instance_id = ? ORDER BY collected_at DESC`.
EXPECTED = [
    (MetricSample, "ix_metric_samples_instance_collected"),
    (SlowQuerySample, "ix_slow_query_samples_instance_collected"),
]


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


@pytest.mark.parametrize("model,index_name", EXPECTED, ids=lambda v: getattr(v, "__name__", v))
def test_the_model_declares_the_composite_index(model, index_name: str):
    index = next((i for i in model.__table__.indexes if i.name == index_name), None)
    assert index is not None, f"{model.__tablename__}: {index_name} tanımlı değil"

    columns = [c.name for c in index.columns]
    assert columns == ["instance_id", "collected_at"], (
        f"kolon SIRASI önemli: eşitlik filtresi (instance_id) önce, sıralama kolonu "
        f"(collected_at) sonra gelmeli — bulunan: {columns}"
    )


@pytest.mark.parametrize("model,index_name", EXPECTED, ids=lambda v: getattr(v, "__name__", v))
async def test_the_index_exists_in_the_live_schema(model, index_name: str):
    """`create_all` yalnızca eksik TABLOLARI oluşturur; var olan bir tabloya sonradan
    tanımlanmış indeksi EKLEMEZ. Bu yüzden SQLite tarafında elle yaratılıyor — o yol
    kopmuşsa geliştirme ortamı sessizce indekssiz kalır."""
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t"),
                {"t": model.__tablename__},
            )
        ).fetchall()

    assert index_name in {r[0] for r in rows}, (
        f"{model.__tablename__} tablosunda {index_name} yok — mevcut: {sorted(r[0] for r in rows)}"
    )


@pytest.mark.parametrize("model,index_name", EXPECTED, ids=lambda v: getattr(v, "__name__", v))
async def test_the_hot_query_plan_uses_the_composite_index(model, index_name: str):
    """Asıl kanıt: planlayıcı gerçekten bu indeksi seçiyor ve SIRALAMA yapmıyor.

    (SQLite planlayıcısı Postgres'inkinden farklı, ama erişim kalıbı aynı: eşitlik + sıralama
    tek bir indeksten karşılanabiliyorsa geçici sıralama adımı ortadan kalkar.)
    """
    query = (
        f"SELECT collected_at FROM {model.__tablename__} "
        "WHERE instance_id = 1 ORDER BY collected_at DESC LIMIT 1"
    )
    async with engine.begin() as conn:
        plan = " ".join(str(r[-1]) for r in (await conn.execute(text(f"EXPLAIN QUERY PLAN {query}"))).fetchall())

    assert index_name in plan, f"plan bileşik indeksi kullanmıyor: {plan}"
    assert "TEMP B-TREE" not in plan.upper(), (
        f"plan hâlâ geçici sıralama yapıyor — indeks işe yaramıyor: {plan}"
    )


def test_the_migration_ships_both_indexes():
    """Yerel SQLite'ta otomatik oluşması yeterli değil: `migrate_schema()` Postgres'te no-op,
    yani migration yazılmazsa CANLIDA indeks hiç oluşmaz — düzeltilen hata da tam olarak
    canlıda yaşanıyordu."""
    assert MIGRATION.exists(), f"migration dosyası yok: {MIGRATION.name}"
    sql = MIGRATION.read_text(encoding="utf-8")

    for _model, index_name in EXPECTED:
        assert index_name in sql, f"migration {index_name} indeksini oluşturmuyor"


def test_the_migration_creates_indexes_without_locking_writes():
    """Tablo milyonlarca satır: normal `CREATE INDEX` tamamlanana kadar tabloya YAZMAYI
    kilitler ve toplama döngüsü durur. Etkin (yorum olmayan) satırlar CONCURRENTLY olmalı."""
    sql = MIGRATION.read_text(encoding="utf-8")
    active = [ln.strip() for ln in sql.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    creates = [ln for ln in active if re.match(r"(?i)^create index", ln)]

    assert creates, "etkin CREATE INDEX satırı yok"
    for line in creates:
        assert "CONCURRENTLY" in line.upper(), f"kilitleyen CREATE INDEX: {line}"


def test_the_migration_is_registered_in_deploy_md():
    """Migration yazılıp DEPLOY.md'ye işlenmezse canlıda uygulanmayı bekler ve 502 sürer."""
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert MIGRATION.name in deploy, "migration DEPLOY.md tablosunda yok"
