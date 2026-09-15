"""Toplama turunun hata sınırı, durum kaydı ve sistemik hata tespiti (Faz 30 İŞ 1).

## Çözdüğü sorun

`collect_all_instances` bütün veritabanları için TEK oturum ve sonda TEK commit
kullanıyordu. Bir veritabanında flush hatası olduğunda oturum geri alınmış duruma
düşüyor, aynı turdaki diğer veritabanları `PendingRollbackError` alıyor ve commit de
düşüyordu. Yani bir veritabanının sorunu, sorunsuz olanların verisini de siliyordu.

Bu canlıda yaşandı: çalıştırılmamış bir migration yüzünden HİÇBİR veritabanı metrik
yazamadı.

## İki ayrı hata sınıfı

Bir veritabanının bağlantısının kopması ile kodun şemayla uyumsuz olması aynı şey değil:

- **`KIND_INSTANCE`** — o veritabanına özel: bağlantı yok, yetki yok, hedefteki bir view
  okunamıyor. Diğer veritabanlarını ilgilendirmez.
- **`KIND_SCHEMA`** — kod, KENDİ veritabanımızın şemasıyla uyumsuz. Bu tek bir
  veritabanının sorunu değil: kurulum eksik (çalıştırılmamış migration). Tek tek
  "şu veritabanı hata verdi" demek, operatörü yanlış yere bakmaya gönderir.

Ayrım bunun için var: sistemik hata, örnek bazlı hata listesi olarak DEĞİL, tek bir
"kod ve şema uyumsuz" uyarısı olarak raporlanıyor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update

from app.collectors.base import classify_connection_error
from app.database import SessionLocal
from app.models import Instance

logger = logging.getLogger(__name__)

KIND_SCHEMA = "schema"
KIND_INSTANCE = "instance"

#: Hata metni bu desenlerden birine uyuyorsa sorun hedefte değil BİZDE: kod bir sütun/tablo
#: bekliyor ama veritabanında yok. Motor bağımsız — PostgreSQL, SQL Server ve SQLite aynı
#: durumu farklı kelimelerle söylüyor.
#:
#: DESEN, alt dize değil: PostgreSQL'in metni "column slow_query_samples.wal_bytes does not
#: exist" — sütun adı aradaki boşluğu dolduruyor, "column does not exist" araması tutmuyordu.
_SCHEMA_PATTERNS = (
    "undefinedcolumn",
    "undefinedtable",
    "no such column",
    "no such table",
    "has no column named",
    "invalid column name",
    "invalid object name",
    "unknown column",
    "column .*does not exist",
    "relation .*does not exist",
)

_SCHEMA_RE = re.compile("|".join(_SCHEMA_PATTERNS))


#: Hata metni kullanıcıya gösteriliyor; sürücü bazen sorgunun tamamını da ekliyor.
_MAX_ERROR_LENGTH = 500


def classify_collection_error(exc: BaseException) -> tuple[str, str]:
    """(kind, kullanıcıya gösterilecek mesaj).

    Şema hatası ÖNCE bakılıyor: `classify_connection_error` "does not exist" gibi bir
    metni "veritabanı bulunamadı" diye yorumlayabilir ve eksik bir sütunu hedefteki bir
    sorun gibi gösterirdi — yanlış yere bakmaya gönderir.
    """
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if _SCHEMA_RE.search(lowered):
        message = (
            "Kod, kendi veritabanımızın şemasıyla uyumsuz: beklenen bir sütun ya da tablo yok. "
            "Büyük olasılıkla çalıştırılmamış bir migration var (DEPLOY.md). "
            f"({text})"
        )
        return KIND_SCHEMA, _trim(message)
    return KIND_INSTANCE, _trim(classify_connection_error(exc))


def _trim(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_LENGTH else text[: _MAX_ERROR_LENGTH - 1] + "…"


async def record_collection_success(session, instance: Instance) -> None:
    """Başarılı toplamada durum alanlarını temizler.

    Aynı oturumda: veriyle BİRLİKTE commit edilmeli, yoksa "son başarılı toplama" yazıp
    veriyi kaydedememek mümkün olurdu.
    """
    instance.last_collect_ok_at = datetime.now(UTC)
    instance.last_collect_error = None
    instance.last_collect_error_at = None
    instance.last_collect_error_kind = None


async def record_collection_failure(instance_id: int, kind: str, message: str) -> None:
    """Hatayı AYRI bir oturumda yazar.

    Hatanın oluştuğu oturum geri alınmış durumda; üzerinden bir şey yazılamaz. Bu yüzden
    durum kaydı kendi oturumunu açıyor ve ORM nesnesi yerine doğrudan UPDATE kullanıyor.

    Yazma işleminin kendisi de başarısız olabilir — hata tam olarak `instances` tablosundaki
    bu sütunların eksikliğiyse. O durumda sessizce geçmiyoruz: log'a düşüyor, ve zaten
    sistemik uyarı ayrıca üretiliyor.
    """
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Instance)
                .where(Instance.id == instance_id)
                .values(
                    last_collect_error=message,
                    last_collect_error_at=datetime.now(UTC),
                    last_collect_error_kind=kind,
                )
            )
            await session.commit()
    except Exception:
        logger.exception(
            "could not record collection failure for instance %s — status columns may be missing",
            instance_id,
        )


@dataclass(frozen=True)
class SystemicNotice:
    """Tek tek veritabanlarının değil, KURULUMUN sorunu."""

    kind: str
    message: str
    affected: int
    since: datetime

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "affected": self.affected,
            "since": self.since.isoformat(),
        }


_notice: SystemicNotice | None = None


def note_cycle(*, schema_failures: int, attempted: int) -> SystemicNotice | None:
    """Bir toplama turunun sonunda çağrılır; sistemik uyarıyı kurar ya da kaldırır.

    `since` KORUNUYOR: uyarı ilk ne zaman görüldüyse o. Her turda tazelenseydi "5 dakikadır
    böyle" ile "üç gündür böyle" ayırt edilemezdi.
    """
    global _notice
    if schema_failures <= 0:
        _notice = None
        return None
    message = (
        f"Kod ile veritabanı şeması uyumsuz: denenen {attempted} veritabanından "
        f"{schema_failures} tanesinde toplama şema hatası verdi. Bu tek tek veritabanlarının "
        "sorunu değil — büyük olasılıkla çalıştırılmamış bir migration var. "
        "DEPLOY.md'deki migration tablosunu kontrol edin."
    )
    _notice = SystemicNotice(
        kind=KIND_SCHEMA,
        message=message,
        affected=schema_failures,
        since=_notice.since if _notice and _notice.kind == KIND_SCHEMA else datetime.now(UTC),
    )
    return _notice


def systemic_notice() -> SystemicNotice | None:
    return _notice


def reset() -> None:
    """Yalnızca testler için: modül seviyesindeki uyarı testler arasında sızmasın."""
    global _notice
    _notice = None
