"""Log yapılandırması (Faz 27 İŞ 2).

SORUN: bekleme örnekleyicisi saniyede bir çalışıyor ve APScheduler her tetiklemeyi INFO
seviyesinde logluyordu — Railway'de **günde 86.400 satır** gürültü. Gerçek hatalar bu yığının
içinde kayboluyor; log'un varlık sebebi de tam olarak onları görebilmek.

Bu modül üç şey yapıyor:

1. **Gürültülü kütüphane logger'larını susturuyor.** APScheduler'ın "Running job ..." /
   "Job executed successfully" satırları saniyede bir işte hiçbir bilgi taşımıyor; işin
   ÇALIŞMADIĞI durum zaten WARNING/ERROR olarak geliyor.
2. **Seviyeyi ortam değişkeniyle ayarlanabilir yapıyor** (`LOG_LEVEL`). Teşhis sırasında
   DEBUG'a çekip sonra geri almak, kod değiştirip yeniden dağıtmaktan hızlı.
3. Hem API hem worker sürecinde AYNI yapılandırmayı kuruyor — ikisinin farklı davranması,
   "worker'da görünen hata API'de görünmüyor" gibi bir teşhis kaybı demekti.
"""

from __future__ import annotations

import logging
import os

#: Sık çalışan işlerin her tetiklemesini yazan kütüphane logger'ları.
#:
#: `apscheduler.executors.default` her iş çalıştırmasında iki satır yazıyor ("Running job",
#: "Job executed successfully"). Bekleme örnekleyicisi saniyede bir çalıştığı için bu tek
#: başına günde ~172 bin satır demek. WARNING'e çekmek iş hatalarını GİZLEMİYOR: APScheduler
#: bir iş exception fırlattığında zaten ERROR yazıyor.
#:
#: `apscheduler.scheduler` iş ekleme/çıkarma satırlarını yazıyor — başlangıçta bir kez
#: yararlı, sonrasında değil; WARNING yeterli.
NOISY_LOGGERS: dict[str, int] = {
    "apscheduler.executors.default": logging.WARNING,
    "apscheduler.executors": logging.WARNING,
    "apscheduler.scheduler": logging.WARNING,
    # httpx her HTTP isteğini INFO'da yazıyor; host-agent yoklamaları ve cluster health
    # probe'ları bunu sürekli tetikliyor.
    "httpx": logging.WARNING,
    # asyncpg bağlantı kurulumunu DEBUG'da yazıyor ama bazı sürümlerde INFO'ya sızıyor.
    "asyncio": logging.WARNING,
}

DEFAULT_LEVEL = "INFO"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def resolve_level(raw: str | None = None) -> int:
    """`LOG_LEVEL` değerini seviyeye çevirir. Tanınmayan değerde INFO'ya düşer.

    Yanlış yazılmış bir seviye yüzünden (ör. `LOG_LEVEL=verbose`) log'un tamamen susması,
    teşhis edilmesi en zor durumlardan biri olurdu.
    """
    if raw is None:
        # Ayar `Settings` üzerinden okunuyor (LOG_LEVEL ortam değişkeni oraya bağlı), ham
        # `os.getenv` değil: .env dosyası ve Railway değişkenleri tek yerden yönetiliyor.
        try:
            from app.config import settings

            raw = settings.log_level
        except Exception:  # pragma: no cover - config yüklenemezse log yine kurulmalı
            raw = os.getenv("LOG_LEVEL", DEFAULT_LEVEL)
    text = (raw or DEFAULT_LEVEL).strip().upper()
    level = logging.getLevelNamesMapping().get(text)
    if level is None:
        logging.getLogger(__name__).warning(
            "LOG_LEVEL='%s' tanınmadı, %s kullanılıyor. Geçerli değerler: %s",
            text, DEFAULT_LEVEL, ", ".join(sorted(logging.getLevelNamesMapping())),
        )
        return logging.getLevelNamesMapping()[DEFAULT_LEVEL]
    return level


def configure_logging(level: str | None = None) -> int:
    """Süreç genelinde log yapılandırması. API ve worker aynı fonksiyonu çağırıyor."""
    resolved = resolve_level(level)
    logging.basicConfig(level=resolved, format=_FORMAT)
    # Yeniden yapılandırma (test, yeniden başlatma) durumunda kök seviyeyi de güncelle:
    # `basicConfig` handler zaten varsa hiçbir şey yapmıyor.
    logging.getLogger().setLevel(resolved)

    for name, noisy_level in NOISY_LOGGERS.items():
        # Gürültülü logger, kök seviyeden DAHA SESSİZ olacak şekilde ayarlanıyor. Kullanıcı
        # bilerek DEBUG'a çektiyse (teşhis) o zaman gürültü de istiyordur — bu yüzden
        # DEBUG'da susturma uygulanmıyor.
        logging.getLogger(name).setLevel(
            noisy_level if resolved > logging.DEBUG else resolved
        )
    return resolved
