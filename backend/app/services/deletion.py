"""Silme öncesi bağımlılık sayımı ve cascade temizliği — TEK kaynak (Faz 23).

**Neden var:** bağımlı tablo listesi elle yazılıyordu ve model listesiyle ayrışmıştı.
`DELETE /api/instances/1` canlıda 500 veriyordu; hemen öncesindeki kontrol ise "bu instance'a
bağlı hiçbir kayıt yok — güvenle silinebilir" diyordu. İkisi çelişiyordu çünkü sayım
`instances.id`'ye bağlı 10 tablonun yalnızca 8'ini biliyordu: Faz 20'de eklenen
`prediction_outcomes` ve Faz 17'de eklenen `daily_state_snapshots` listeye hiç girmemişti.

Bu, projede üçüncü kez görülen "elle tutulan liste sessizce ayrıştı" hatası
(bkz. `PredictionOut.advice`, `ReportFindingOut.facts`). Bu yüzden liste artık ELLE
YAZILMIYOR: SQLAlchemy metadata'sından türetiliyor. Yeni bir tablo `instances.id`'ye foreign
key koyduğu anda sayıma ve cascade'e kendiliğinden dahil oluyor.

**Neden yerelde yakalanmadı:** SQLite foreign key zorlamasını varsayılan olarak KAPALI tutuyor,
yani yerelde ve testlerde silme sessizce başarılı olup geride öksüz satır bırakıyordu. Postgres
her zaman zorluyor. `database.py` artık SQLite'ta da `PRAGMA foreign_keys=ON` yapıyor.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Table, delete as sa_delete, func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base

# Kullanıcıya gösterilecek tablo adları. Listede olmayan bir tablo ham adıyla gösterilir —
# eksik çeviri yüzünden bir bağımlılığın SAKLANMASI kabul edilemez, bilgi eksik görünmesi
# tercih edilir.
TABLE_LABELS: dict[str, str] = {
    "metric_samples": "metrik örneği",
    "slow_query_samples": "yavaş sorgu örneği",
    "alert_rules": "alarm kuralı",
    "alert_events": "alarm olayı",
    "prediction_insights": "tahmin",
    "prediction_outcomes": "tahmin doğruluk kaydı",
    "metric_rollup_daily": "günlük metrik özeti",
    "schema_object_daily_samples": "şema nesnesi örneği",
    "daily_state_snapshots": "günlük durum fotoğrafı",
    "nodes": "düğüm",
    "group_health_snapshots": "grup sağlık görüntüsü",
    "instances": "instance",
    "applications": "uygulama",
    "database_groups": "veritabanı grubu",
    "servers": "sunucu",
    "report_findings": "rapor bulgusu",
    "health_reports": "sağlık raporu",
}


@dataclass
class Dependent:
    """`<table>.<column>` üzerinden hedefe bağlı kayıtlar."""

    table: str
    column: str
    count: int
    # FK nullable ise bağlantı KOPARILABİLİR (kayıt yaşamaya devam eder); değilse kaydın
    # kendisi silinmeli.
    detachable: bool

    @property
    def label(self) -> str:
        return TABLE_LABELS.get(self.table, self.table)


def dependent_columns(target_table: str) -> list[tuple[Table, str, bool]]:
    """`target_table.id`'ye foreign key ile bağlı (tablo, kolon, nullable) üçlüleri.

    Elle tutulan bir listeye göre kritik farkı: yeni bir tablo eklendiğinde burayı güncellemek
    GEREKMİYOR — unutulması mümkün değil.
    """
    # `Base.metadata` yalnızca modeller import edildikten SONRA dolu. Bu import olmadan
    # fonksiyon sessizce boş liste döner — yani "bağlı kayıt yok" der ve düzeltmeye
    # çalıştığımız hatanın aynısını üretir. Modül seviyesinde import etmek döngüsel bağımlılık
    # yaratıyor (models -> database -> ... ), bu yüzden burada.
    from app import models  # noqa: F401

    if not Base.metadata.tables:  # pragma: no cover - import zinciri bozulursa
        raise RuntimeError("model metadata boş — bağımlılık taraması güvenilir değil")

    found: list[tuple[Table, str, bool]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.target_fullname == f"{target_table}.id":
                    # Kendine referans (ör. health_reports.previous_report_id) sayılmaz:
                    # aynı tablonun kaydını silerken kendi zincirini bağımlılık saymak
                    # kullanıcıya anlamsız gelir ve ORM zaten hallediyor.
                    if table.name != target_table:
                        found.append((table, column.name, bool(column.nullable)))
    return found


async def collect_dependents(
    session: AsyncSession, target_table: str, target_id: int
) -> list[Dependent]:
    """Bağlı kayıtları sayar. Sayısı sıfır olanlar da döner — çağıran taraf "hangi tablolar
    kontrol edildi" bilgisini gösterebilsin."""
    results: list[Dependent] = []
    for table, column, nullable in dependent_columns(target_table):
        count = int(
            (
                await session.execute(
                    select(func.count()).select_from(table).where(table.c[column] == target_id)
                )
            ).scalar_one()
        )
        results.append(
            Dependent(table=table.name, column=column, count=count, detachable=nullable)
        )
    return results


def blocking(dependents: list[Dependent]) -> list[Dependent]:
    """Silmeyi gerçekten engelleyenler — kaydı olanlar."""
    return [d for d in dependents if d.count > 0]


def describe(dependents: list[Dependent]) -> str:
    """Kullanıcıya gösterilecek özet: hangi tabloda kaç kayıt engelliyor."""
    parts = [f"{d.count} {d.label}" for d in sorted(blocking(dependents), key=lambda d: -d.count)]
    return ", ".join(parts)


# Özyineleme derinliği sınırı. Şemanın en derin zinciri müşteri → uygulama → grup → düğüm
# (4); sınır bol payla konuldu ki beklenmeyen bir döngüde yığın taşmasın.
_MAX_DEPTH = 8


async def clear_dependents(
    session: AsyncSession, target_table: str, target_id: int, *, _depth: int = 0
) -> int:
    """Cascade temizliği: nullable bağlantılar KOPARILIR, zorunlu olanlar SİLİNİR.

    Kural FK'nın kendisinden geliyor: `nullable=True` ise kayıt hedefe bağlı olmadan da
    anlamlı (ör. `nodes.instance_id` — düğüm cluster topolojisinin parçası, veritabanı
    kaydının değil); `nullable=False` ise kaydın hedef olmadan varlığı yok.

    **Özyinelemeli**, çünkü bağımlılar kendileri de üst kayıt olabiliyor: bir müşteriyi silmek
    uygulamalarını, onlar da gruplarını, onlar da düğümlerini gerektiriyor. Düz bir silme
    zincirin ortasında foreign key ihlaline düşerdi.

    Commit ETMEZ — çağıran taraf silmeyle aynı transaction'da yapsın.
    """
    if _depth > _MAX_DEPTH:  # pragma: no cover - şemada böyle bir zincir yok
        raise RuntimeError(f"bağımlılık zinciri beklenenden derin: {target_table}")

    affected = 0
    for table, column, nullable in dependent_columns(target_table):
        if nullable:
            result = await session.execute(
                sa_update(table).where(table.c[column] == target_id).values(**{column: None})
            )
            affected += result.rowcount or 0
            continue

        # Zorunlu bağ: kaydın kendisi silinecek. Önce ONUN bağımlıları temizlenmeli.
        child_ids = [
            row[0]
            for row in (
                await session.execute(select(table.c.id).where(table.c[column] == target_id))
            ).all()
        ]
        for child_id in child_ids:
            affected += await clear_dependents(session, table.name, child_id, _depth=_depth + 1)
        if child_ids:
            result = await session.execute(sa_delete(table).where(table.c[column] == target_id))
            affected += result.rowcount or 0
    return affected


async def commit_or_conflict(
    session: AsyncSession, target_table: str, target_id: int, label: str
) -> None:
    """Silmeyi commit eder; foreign key ihlalinde 500 yerine ANLAŞILIR bir 409 döner.

    Bu son savunma hattı: sayım artık metadata'dan türetildiği için teorik olarak buraya
    düşülmemeli. Ama düşülürse kullanıcı ham bir 500 görmemeli — hangi tabloda kaç kayıt
    engelliyorsa o yazılmalı. (Canlıdaki hatanın kullanıcıya yansıması "Sunucuya
    ulaşılamıyor" idi; 500 yanıtında CORS başlığı olmadığı için tarayıcı yanıtı hiç
    okuyamıyordu.)
    """
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        dependents = await collect_dependents(session, target_table, target_id)
        detail = describe(dependents)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{label} silinemedi — bağlı kayıtlar var ({detail})."
                if detail
                else (
                    f"{label} silinemedi: veritabanı bir bütünlük kısıtını reddetti. "
                    f"Ayrıntı: {type(exc.orig).__name__}"
                )
            ),
        ) from exc
