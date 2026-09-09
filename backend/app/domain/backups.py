"""Yedek izleme alan tanımları (Faz 28 İŞ 1).

dbace'te yedek izleme hiç yoktu. Bir DBA aracında bu temel direklerden biri: bankada ilk
sorulacak sorulardan biri "son yedek ne zaman alındı".

TEMEL DÜRÜSTLÜK KURALI: **yedek alındığı VARSAYILMAZ.** Bir yedek kaydı bulunamadıysa bu
"yedek yok" demek değil, "dbace bulamadı" demektir — ikisi çok farklı ve ikincisini birincisi
gibi sunmak, gerçekten yedeği olan bir kurumu paniğe, olmayanı ise sahte güvene sürükler.
Bu yüzden her sonda HANGİ YÖNTEMLERİN denendiği kaydediliyor ve bulunamadığında nasıl
yapılandırılacağı yazılıyor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BackupType(StrEnum):
    """Yedek türleri. PostgreSQL ve SQL Server'ın kavramları ortak adlara indirgeniyor."""

    FULL = "full"
    DIFFERENTIAL = "differential"
    LOG = "log"
    #: PostgreSQL'de sürekli WAL arşivleme — tek tek "yedek" değil, kesintisiz bir akış.
    #: SQL Server'daki log yedeğinin karşılığı sayılabilir ama olay değil DURUM olduğu için
    #: ayrı tutuluyor.
    WAL_ARCHIVE = "wal_archive"
    #: `pg_basebackup` ile alınan taban yedek.
    BASE_BACKUP = "base_backup"


class BackupStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    RUNNING = "running"


class BackupSource(StrEnum):
    """Yedek bilgisinin NEREDEN okunduğu. Kullanıcıya gösteriliyor: aynı sunucuda iki farklı
    kaynak farklı şey söyleyebilir (ör. pgBackRest yedek alıyor ama arşivleme durmuş)."""

    MSDB = "msdb"                       # SQL Server: msdb.dbo.backupset
    PG_STAT_ARCHIVER = "pg_stat_archiver"
    PGBACKREST = "pgbackrest"
    BARMAN = "barman"
    WAL_G = "wal_g"
    PG_BASEBACKUP = "pg_basebackup"     # pg_stat_progress_basebackup (devam eden)


#: Kullanıcıya görünen adlar.
SOURCE_LABELS: dict[str, str] = {
    BackupSource.MSDB: "SQL Server msdb yedek geçmişi",
    BackupSource.PG_STAT_ARCHIVER: "PostgreSQL WAL arşivleyici",
    BackupSource.PGBACKREST: "pgBackRest",
    BackupSource.BARMAN: "Barman",
    BackupSource.WAL_G: "WAL-G",
    BackupSource.PG_BASEBACKUP: "pg_basebackup (devam eden)",
}

TYPE_LABELS: dict[str, str] = {
    BackupType.FULL: "Tam yedek",
    BackupType.DIFFERENTIAL: "Fark yedeği",
    BackupType.LOG: "Log yedeği",
    BackupType.WAL_ARCHIVE: "WAL arşivi",
    BackupType.BASE_BACKUP: "Taban yedek",
}


@dataclass(frozen=True)
class BackupAgeThreshold:
    """Bir yedek türünün ne kadar eskiyebileceği."""

    warning_hours: float
    critical_hours: float


#: VARSAYILAN EŞİKLER — instance başına `options.backup_thresholds` ile değiştirilebiliyor.
#:
#: Değerler kurumsal pratiğe göre seçildi: haftalık tam yedek yaygın bir taban, log yedeği ise
#: kurtarma noktası hedefini (RPO) doğrudan belirliyor. Bir saatlik log yedeği, en fazla bir
#: saatlik veri kaybı demek; bu yüzden log eşiği tam yedekten çok daha dar.
DEFAULT_THRESHOLDS: dict[str, BackupAgeThreshold] = {
    BackupType.FULL: BackupAgeThreshold(warning_hours=24 * 8, critical_hours=24 * 14),
    BackupType.DIFFERENTIAL: BackupAgeThreshold(warning_hours=36, critical_hours=72),
    BackupType.LOG: BackupAgeThreshold(warning_hours=1, critical_hours=4),
    BackupType.WAL_ARCHIVE: BackupAgeThreshold(warning_hours=1, critical_hours=4),
    BackupType.BASE_BACKUP: BackupAgeThreshold(warning_hours=24 * 8, critical_hours=24 * 14),
}


def thresholds_for(instance_options: dict | None) -> dict[str, BackupAgeThreshold]:
    """Instance'a özgü eşikler; tanımlı olmayanlar varsayılandan geliyor.

    Eşikler instance BAŞINA ayarlanabilir olmalı çünkü yedek politikası veritabanına göre
    değişiyor: bir raporlama veritabanı için günlük tam yedek yeterliyken, ödeme sistemi için
    15 dakikalık log yedeği bile gevşek kalabilir. Tek bir global eşik, ya birini gereksiz
    alarma boğar ya da diğerini korumasız bırakır.
    """
    overrides = ((instance_options or {}).get("backup_thresholds") or {})
    resolved: dict[str, BackupAgeThreshold] = {}
    for backup_type, default in DEFAULT_THRESHOLDS.items():
        override = overrides.get(str(backup_type)) or {}
        try:
            warning = float(override.get("warning_hours", default.warning_hours))
            critical = float(override.get("critical_hours", default.critical_hours))
        except (TypeError, ValueError):
            warning, critical = default.warning_hours, default.critical_hours
        # Kritik eşiğin uyarıdan küçük olması mantıksız; yanlış yapılandırmayı sessizce
        # kabul etmek yerine düzeltiyoruz (aksi halde kritik hiç tetiklenmezdi).
        if critical < warning:
            critical = warning
        resolved[str(backup_type)] = BackupAgeThreshold(warning, critical)
    return resolved


#: Devam eden yedeğin süresi, son başarılı yedeklerin ortalamasının bu katını aşarsa anormal.
DURATION_ANOMALY_MULTIPLIER = 2.0

#: Ortalamayla karşılaştırmanın YANINDA statik bir taban.
#:
#: NEDEN ŞART: süreler küçükken oran yanıltıcı. 4 saniye süren bir yedek 9 saniyeye çıktığında
#: "2 kat yavaşladı" doğru ama anlamsız — kimse 9 saniyelik yedek için uyandırılmamalı.
#: Statik taban bu yanlış alarmı kesiyor; ikisi BİRLİKTE aranıyor.
DURATION_ANOMALY_MIN_SECONDS = 900.0

#: Ortalama hesabı için gereken en az başarılı yedek sayısı. Altındaysa "geçmiş yok",
#: karşılaştırma yapılmıyor ve bu söyleniyor.
DURATION_BASELINE_MIN_SAMPLES = 3

#: Ortalama şu kadar son yedekten hesaplanıyor.
DURATION_BASELINE_SAMPLES = 5

#: Yedek boyutu bu oranda değişirse anormal (ani büyüme ya da küçülme).
#: Küçülme en az büyüme kadar önemli: yarıya inen bir yedek genelde eksik yedek demek.
SIZE_ANOMALY_RATIO = 0.5

#: Başarısız yedek durumu bu süre sonunda sıfırlanıyor — ekran sürekli kırmızı kalmasın.
#: Kayıt SİLİNMİYOR, yalnızca "güncel durum" hesabına girmiyor: geçmişte olmuş bir başarısızlık
#: sonsuza kadar alarm üretirse alarm körlüğü yaratır, ama izi kaybolursa da denetim yapılamaz.
FAILURE_STICKY_HOURS = 24.0

def failure_sticky_hours(instance_options: dict | None) -> float:
    """Başarısız yedek durumunun ne kadar süre "güncel" sayılacağı.

    Instance başına ayarlanabiliyor (`options.backup_failure_sticky_hours`) çünkü yedek
    sıklığı kuruma göre değişiyor: 15 dakikada bir log yedeği alan bir sistemde 24 saatlik
    yapışkanlık çok uzun, haftalık yedek alan bir sistemde ise 24 saat çok kısa.
    """
    try:
        value = float((instance_options or {}).get("backup_failure_sticky_hours", FAILURE_STICKY_HOURS))
    except (TypeError, ValueError):
        return FAILURE_STICKY_HOURS
    # Sıfır ya da negatif bir değer, başarısızlığı hiç göstermemek demek olurdu — sessizce
    # kabul etmek yerine varsayılana dönülüyor.
    return value if value > 0 else FAILURE_STICKY_HOURS


#: Replikasyon slotu bu kadar WAL biriktirdiyse disk riski var.
#: Kullanılmayan bir slot WAL'ı sonsuza kadar tutar ve diski doldurur — bu, PostgreSQL'de
#: en sık görülen "disk doldu" sebeplerinden biri.
SLOT_RETAINED_WAL_WARNING_BYTES = 10 * 1024**3   # 10 GB
SLOT_RETAINED_WAL_CRITICAL_BYTES = 50 * 1024**3  # 50 GB


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def type_label(backup_type: str) -> str:
    return TYPE_LABELS.get(backup_type, backup_type)


#: Yedek bulunamadığında kullanıcıya verilecek yapılandırma rehberi. Motor başına.
#:
#: "Yedek bulunamadı" demek yetmez: kullanıcı dbace'in NEREYE baktığını bilmeli ki ya
#: yapılandırmayı düzeltsin ya da gerçekten yedeği olmadığını anlasın.
NOT_FOUND_GUIDANCE: dict[str, str] = {
    "postgresql": (
        "PostgreSQL'de yedek bilgisi tek bir yerden okunamıyor; dbace şu kaynaklara bakıyor: "
        "(1) `pg_stat_archiver` — WAL arşivleme açıksa buradan okunur, `archive_mode = on` ve "
        "`archive_command` ayarlı olmalı; (2) `pg_stat_progress_basebackup` — devam eden taban "
        "yedek; (3) host-agent üzerinden pgBackRest / Barman / WAL-G durum çıktısı — bunun için "
        "instance'ın `agent_url` seçeneği tanımlı ve agent'ta yedek aracı kurulu olmalı. "
        "Hiçbiri bulunamadıysa dbace yedeğinizin olup olmadığını BİLEMİYOR demektir."
    ),
    "sqlserver": (
        "SQL Server'da yedek geçmişi `msdb.dbo.backupset` tablosundan okunuyor. Kayıt "
        "görünmüyorsa: bağlanan login'in msdb üzerinde okuma yetkisi olmayabilir "
        "(`GRANT SELECT ON msdb.dbo.backupset TO <login>` ya da `db_datareader` msdb'de), "
        "ya da yedek geçmişi temizlenmiş olabilir (`sp_delete_backuphistory`). Yedekler üçüncü "
        "parti bir araçla (Veeam, Commvault) alınıyorsa ve VDI/anlık görüntü kullanıyorsa "
        "msdb'ye kayıt düşmeyebilir."
    ),
    "mongodb": (
        "MongoDB için yedek izleme desteklenmiyor: mongodump/Ops Manager yedekleri "
        "veritabanının kendi kataloğunda iz bırakmıyor ve dbace'in okuyabileceği standart bir "
        "kaynak yok."
    ),
}
