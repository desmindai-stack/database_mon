"""Bekleme (wait event) taksonomisi — TEK GERÇEKLİK KAYNAĞI (Faz 25 İŞ 1).

pg_stat_statements bize "bu sorgu 206 ms sürdü" diyor; bu sürenin NEREDE geçtiğini
söylemiyor. Bekleme analizi tam olarak bunu ekler: her örnekte aktif oturumun ne beklediği
kaydedilir, dakikalık toplanır ve "sistem neyi bekliyor" sorusu ölçümle cevaplanır.

İki motor iki farklı sözlük kullanıyor (PostgreSQL `wait_event_type`, SQL Server `wait_type`).
Bu modül ikisini ORTAK bir kategori kümesine indirger; grafik, öneri üretimi ve rapor aynı
kategorileri konuşur. Kategori eşlemesi burada tek yerde durur — iki modülün aynı beklemeyi
farklı sınıflandırması, projede daha önce yaşanan "aynı veriyi iki yerde ayrı hesaplama"
tuzağının aynısı olurdu.

EN ÖNEMLİ KURAL — beklemenin YOKLUĞU da bir bilgidir:
PostgreSQL'de `state='active'` ve `wait_event_type IS NULL` ise oturum hiçbir şey beklemiyor,
yani CPU'da çalışıyor. SQL Server'da `dm_exec_requests.wait_type IS NULL` aynı anlama gelir.
Bu satırları atmak, yükün en büyük parçasını görünmez yapardı.
"""

from __future__ import annotations

from enum import StrEnum


class WaitCategory(StrEnum):
    """Ortak bekleme kategorileri. Değerler API'de ve arayüzde aynen kullanılıyor."""

    CPU = "cpu"
    IO = "io"
    LOCK = "lock"
    LWLOCK = "lwlock"
    CLIENT = "client"
    IPC = "ipc"
    TIMEOUT = "timeout"
    BUFFER_PIN = "buffer_pin"
    ACTIVITY = "activity"
    EXTENSION = "extension"
    MEMORY = "memory"
    OTHER = "other"


#: Grafikte yığılma sırası ve listelerde gösterim sırası. Kullanıcıya en çok şey anlatan
#: kategoriler üstte: önce CPU (yapılan iş), sonra beklemeler etki sırasına göre.
CATEGORY_ORDER: tuple[WaitCategory, ...] = (
    WaitCategory.CPU,
    WaitCategory.IO,
    WaitCategory.LOCK,
    WaitCategory.LWLOCK,
    WaitCategory.MEMORY,
    WaitCategory.IPC,
    WaitCategory.BUFFER_PIN,
    WaitCategory.TIMEOUT,
    WaitCategory.CLIENT,
    WaitCategory.EXTENSION,
    WaitCategory.ACTIVITY,
    WaitCategory.OTHER,
)

#: Kullanıcıya görünen Türkçe adlar (arayüz ve rapor metinleri buradan besleniyor).
CATEGORY_LABELS: dict[WaitCategory, str] = {
    WaitCategory.CPU: "CPU",
    WaitCategory.IO: "Disk G/Ç",
    WaitCategory.LOCK: "Kilit",
    WaitCategory.LWLOCK: "Hafif kilit (LWLock)",
    WaitCategory.CLIENT: "İstemci bekleniyor",
    WaitCategory.IPC: "Süreçler arası (paralellik)",
    WaitCategory.TIMEOUT: "Zaman aşımı/bekleme",
    WaitCategory.BUFFER_PIN: "Buffer pin",
    WaitCategory.ACTIVITY: "Arka plan boşta",
    WaitCategory.EXTENSION: "Uzantı",
    WaitCategory.MEMORY: "Bellek (grant/work_mem)",
    WaitCategory.OTHER: "Diğer",
}

#: Bir kategorinin ne anlama geldiğinin tek cümlelik açıklaması. Arayüzde ipucu olarak,
#: öneride gerekçe olarak kullanılıyor — kullanıcı "LWLock ne demek" diye aramak zorunda
#: kalmasın.
CATEGORY_MEANING: dict[WaitCategory, str] = {
    WaitCategory.CPU: "Oturum hiçbir şey beklemiyor, işlemcide çalışıyor.",
    WaitCategory.IO: "Veri diskten okunuyor ya da diske yazılıyor; cache'te bulunamadı.",
    WaitCategory.LOCK: "Başka bir transaction'ın tuttuğu kilit bekleniyor.",
    WaitCategory.LWLOCK: "Veritabanının kendi iç yapılarına erişim sırası bekleniyor.",
    WaitCategory.CLIENT: "Veritabanı hazır, uygulamadan veri/komut bekliyor.",
    WaitCategory.IPC: "Paralel işçiler ya da yardımcı süreçler arasında bekleme.",
    WaitCategory.TIMEOUT: "Kasıtlı bir bekleme (uyku, gecikme ayarı).",
    WaitCategory.BUFFER_PIN: "Bellekteki bir sayfanın serbest kalması bekleniyor.",
    WaitCategory.ACTIVITY: "Arka plan süreci iş bekliyor — yük değil, boşta durum.",
    WaitCategory.EXTENSION: "Bir uzantının kendi bekleme noktası.",
    WaitCategory.MEMORY: "Sorgu çalışmak için bellek tahsisi bekliyor.",
    WaitCategory.OTHER: "Sınıflandırılamayan bekleme.",
}

#: Yük SAYILMAYAN kategoriler. `Activity` PostgreSQL'de arka plan süreçlerinin boşta bekleme
#: noktasıdır (walwriter'ın `WalWriterMain` gibi); bunu veritabanı yükü saymak AAS'i sabit bir
#: taban değerle şişirir ve grafiği yalancı yapar. Örnekleyici zaten `client backend` dışını
#: dışlıyor ama sürüm/motor farkları için savunma burada da duruyor.
IDLE_CATEGORIES: frozenset[WaitCategory] = frozenset({WaitCategory.ACTIVITY})

# PostgreSQL `pg_stat_activity.wait_event_type` değerleri sabittir ve doğrudan eşlenir.
_PG_TYPE_MAP: dict[str, WaitCategory] = {
    "IO": WaitCategory.IO,
    "Lock": WaitCategory.LOCK,
    "LWLock": WaitCategory.LWLOCK,
    "Client": WaitCategory.CLIENT,
    "IPC": WaitCategory.IPC,
    "Timeout": WaitCategory.TIMEOUT,
    "BufferPin": WaitCategory.BUFFER_PIN,
    "Activity": WaitCategory.ACTIVITY,
    "Extension": WaitCategory.EXTENSION,
}

# SQL Server'da `wait_type` yüzlerce değer alabilir ve sabit bir tip sütunu yoktur; bu yüzden
# eşleme ÖNEK tabanlı. Sıra önemli: uzun önek önce denenir (PAGEIOLATCH_ ile PAGELATCH_ gibi).
_MSSQL_PREFIX_MAP: tuple[tuple[str, WaitCategory], ...] = (
    # Disk G/Ç — veri sayfası okuma, log yazma, yedek G/Ç.
    ("PAGEIOLATCH_", WaitCategory.IO),
    ("WRITELOG", WaitCategory.IO),
    ("IO_COMPLETION", WaitCategory.IO),
    ("ASYNC_IO_COMPLETION", WaitCategory.IO),
    ("BACKUPIO", WaitCategory.IO),
    ("LOGBUFFER", WaitCategory.IO),
    # Kilitler.
    ("LCK_M_", WaitCategory.LOCK),
    # Latch'ler PostgreSQL'in LWLock'unun karşılığı: kısa süreli iç yapı korumaları.
    ("PAGELATCH_", WaitCategory.LWLOCK),
    ("LATCH_", WaitCategory.LWLOCK),
    ("TRAN_MARKLATCH_", WaitCategory.LWLOCK),
    # Uygulama tarafı: sonuç kümesi istemci tarafından çekilmiyor.
    ("ASYNC_NETWORK_IO", WaitCategory.CLIENT),
    ("NETWORK_IO", WaitCategory.CLIENT),
    # Paralellik.
    ("CXPACKET", WaitCategory.IPC),
    ("CXCONSUMER", WaitCategory.IPC),
    ("EXCHANGE", WaitCategory.IPC),
    ("HTBUILD", WaitCategory.IPC),
    # Bellek tahsisi (memory grant) — PostgreSQL'de doğrudan karşılığı yok.
    ("RESOURCE_SEMAPHORE", WaitCategory.MEMORY),
    ("CMEMTHREAD", WaitCategory.MEMORY),
    ("MEMORY_ALLOCATION_EXT", WaitCategory.MEMORY),
    # Kasıtlı bekleme.
    ("WAITFOR", WaitCategory.TIMEOUT),
    ("SLEEP_", WaitCategory.TIMEOUT),
    # SOS_SCHEDULER_YIELD bir BEKLEME gibi görünse de anlamı "CPU kotasını doldurdu, sıraya
    # girdi" — yani CPU baskısıdır, kaynağı işlemcidir. IO ya da kilit sanmak, CPU sorununu
    # tamamen yanlış yerde aratır.
    ("SOS_SCHEDULER_YIELD", WaitCategory.CPU),
)


def classify_postgres_wait(wait_event_type: str | None) -> WaitCategory:
    """`pg_stat_activity.wait_event_type` → ortak kategori.

    `None` = beklemiyor = CPU'da. Bu, veritabanı yükünün en büyük bileşenidir ve
    "bekleme yok" diye atılamaz.
    """
    if wait_event_type is None or wait_event_type == "":
        return WaitCategory.CPU
    return _PG_TYPE_MAP.get(wait_event_type, WaitCategory.OTHER)


def classify_sqlserver_wait(wait_type: str | None) -> WaitCategory:
    """`sys.dm_exec_requests.wait_type` → ortak kategori. `None` = CPU'da çalışıyor."""
    if wait_type is None or wait_type == "":
        return WaitCategory.CPU
    upper = wait_type.upper()
    for prefix, category in _MSSQL_PREFIX_MAP:
        if upper.startswith(prefix):
            return category
    return WaitCategory.OTHER


def category_label(category: str) -> str:
    try:
        return CATEGORY_LABELS[WaitCategory(category)]
    except ValueError:
        return category


def category_meaning(category: str) -> str:
    try:
        return CATEGORY_MEANING[WaitCategory(category)]
    except ValueError:
        return CATEGORY_MEANING[WaitCategory.OTHER]


def is_load_bearing(category: str) -> bool:
    """Bu kategori veritabanı YÜKÜ sayılır mı? (AAS hesabına girer mi)"""
    try:
        return WaitCategory(category) not in IDLE_CATEGORIES
    except ValueError:
        return True
