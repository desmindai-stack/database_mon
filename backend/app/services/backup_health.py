"""Yedek durumunun değerlendirilmesi (Faz 28 İŞ 1b).

İŞ 1a toplamayı yaptı: `BackupRecord` satırları ve `BackupProbe` sondası. Bu modül o ham
veriden **karar** üretiyor — yedek yeterince yeni mi, bir şey bozuldu mu, ve en önemlisi
dbace bunu gerçekten BİLİYOR mu.

Modül saf: veritabanına gitmiyor, kendisine verilen kayıtlar üzerinden çalışıyor. Sebebi
test edilebilirlik değil sadece — rapor bölümü, ileride eklenecek bir arayüz paneli ve
alarm motoru **aynı fonksiyondan** beslenmeli. Ayrı hesaplama = ayrı sonuç = güven kaybı.

## En kritik ayrım: "yedek yok" ≠ "dbace bulamadı"

Bir izleme aracının verebileceği en pahalı iki yanlış cevap şunlar:

1. Yedeği olan kuruma "yedeğiniz yok" demek → gereksiz panik, güven kaybı.
2. Yedeği OLMAYAN kuruma sessiz kalmak → felaket anında geri dönüş yok.

İkincisi kıyas kabul etmeyecek kadar pahalı, ama birincisini de üretmemek gerekiyor. Çözüm
üçüncü bir durum: **belirlenemedi**. `_detection_is_conclusive` bunu ayırıyor — dbace
yalnızca yetkili kaynağı gerçekten okuyabildiyse "yedek yok" diyor; okuyamadıysa neyin
engellediğini söylüyor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from app.domain.backups import (
    DURATION_ANOMALY_MIN_SECONDS,
    DURATION_ANOMALY_MULTIPLIER,
    DURATION_BASELINE_MIN_SAMPLES,
    DURATION_BASELINE_SAMPLES,
    NOT_FOUND_GUIDANCE,
    SIZE_ANOMALY_RATIO,
    SLOT_RETAINED_WAL_CRITICAL_BYTES,
    SLOT_RETAINED_WAL_WARNING_BYTES,
    BackupSource,
    BackupStatus,
    BackupType,
    failure_sticky_hours,
    source_label,
    thresholds_for,
    type_label,
)
from app.services.advice import Advice, AdviceStep

#: "Tam yedek" sayılan türler. PostgreSQL tarafında pgBackRest `full`, WAL-G/Barman ise taban
#: yedek üretiyor; ikisi de "buradan geri dönülebilir" anlamına geldiği için aynı kefede.
FULL_EQUIVALENT_TYPES = (BackupType.FULL, BackupType.BASE_BACKUP)

#: "Log yedeği" sayılan türler. PostgreSQL'de karşılığı sürekli WAL arşivlemesi.
LOG_EQUIVALENT_TYPES = (BackupType.LOG, BackupType.WAL_ARCHIVE)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite naive, Postgres aware datetime döndürüyor — tek noktada UTC'ye sabitleniyor."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _age_hours(moment: datetime | None, now: datetime) -> float | None:
    moment = _as_utc(moment)
    if moment is None:
        return None
    return max((now - moment).total_seconds() / 3600.0, 0.0)


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "bilinmiyor"
    if hours < 1:
        return f"{hours * 60:.0f} dakika"
    if hours < 48:
        return f"{hours:.1f} saat"
    return f"{hours / 24:.1f} gün"


def _fmt_bytes(value: float | None) -> str:
    if value is None:
        return "bilinmiyor"
    step = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(step) < 1024 or unit == "TB":
            return f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} TB"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "bilinmiyor"
    if seconds < 90:
        return f"{seconds:.0f} sn"
    if seconds < 5400:
        return f"{seconds / 60:.1f} dk"
    return f"{seconds / 3600:.1f} saat"


@dataclass(frozen=True)
class BackupIssue:
    """Tek bir yedek bulgusu. Rapor bölümü bunu `FindingDraft`'a çeviriyor."""

    key: str
    severity: str  # critical | warning | info
    title: str
    detail: str
    evidence: dict[str, Any]
    advice: Advice
    facts: list[dict[str, Any]] = field(default_factory=list)
    database_name: str | None = None


@dataclass
class BackupAssessment:
    """Bir veritabanının yedek durumu."""

    instance_id: int
    instance_name: str
    engine: str

    #: dbace yedek kaynağını gerçekten okuyabildi mi? False ise "yedek yok" DENMİYOR.
    conclusive: bool
    #: Herhangi bir yedek izi bulundu mu?
    detected: bool

    methods_checked: list[str] = field(default_factory=list)
    methods_found: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    last_full_at: datetime | None = None
    last_full_age_hours: float | None = None
    last_log_at: datetime | None = None
    last_log_age_hours: float | None = None

    #: Eşiklere göre uygunluk. **None = belirlenemedi** — "uygun değil" ile karıştırılmamalı.
    sla_ok: bool | None = None

    status: str = "unknown"  # ok | info | warning | critical | unknown
    issues: list[BackupIssue] = field(default_factory=list)
    running: list[dict[str, Any]] = field(default_factory=list)
    #: Always On: yedeği hangi replika alıyor. Tek düğüm bekleniyor; birden fazlaysa yedek
    #: politikası dağınık demektir ve geri dönüşte hangi düğümdeki dosyanın gerektiği belirsiz.
    backup_servers: list[str] = field(default_factory=list)
    summary: str = ""

    def worst_severity(self) -> str:
        for level in ("critical", "warning", "info"):
            if any(i.severity == level for i in self.issues):
                return level
        return "ok"


# --- Yardımcı seçiciler ---------------------------------------------------------------------


def _successful(records: Iterable[Any], types: tuple[str, ...]) -> list[Any]:
    rows = [
        r
        for r in records
        if str(r.status) == str(BackupStatus.SUCCESS) and str(r.backup_type) in {str(t) for t in types}
    ]
    rows.sort(key=lambda r: _as_utc(r.started_at) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


def _latest_time(rows: list[Any]) -> datetime | None:
    if not rows:
        return None
    # Bitiş zamanı varsa o kullanılıyor: "yedeğim ne kadar eski" sorusunun cevabı yedeğin
    # BİTTİĞİ an. Başlangıcı almak, 6 saat süren bir yedeği 6 saat daha taze gösterirdi.
    finished = _as_utc(rows[0].finished_at)
    return finished or _as_utc(rows[0].started_at)


def _severity_for_age(age_hours: float | None, threshold) -> str | None:
    if age_hours is None:
        return None
    if age_hours >= threshold.critical_hours:
        return "critical"
    if age_hours >= threshold.warning_hours:
        return "warning"
    return None


# --- Öneriler -------------------------------------------------------------------------------


def _age_advice(engine: str, kind: str, age_hours: float | None, limit_hours: float) -> Advice:
    """Yedek eskidiğinde ne yapılacağı (beş parçalı standart)."""
    if engine == "sqlserver":
        if kind == "log":
            return Advice(
                title="Log yedeği zincirini yeniden çalıştırın",
                why=(
                    f"Son log yedeği {_fmt_age(age_hours)} önce alınmış; eşik "
                    f"{_fmt_age(limit_hours)}. Log yedeği aralığı, bir arıza anında KAYBEDİLECEK "
                    "veri miktarını doğrudan belirliyor: iki log yedeği arasında geçen süre ne "
                    "kadarsa kayıp da o kadar. Ayrıca FULL recovery model'de log yedeği "
                    "alınmazsa transaction log dosyası büyümeye devam eder ve diski doldurur."
                ),
                steps=[
                    AdviceStep(
                        "Log yedeği işinin neden çalışmadığını bulun (SQL Server Agent iş geçmişi).",
                        "SELECT j.name, h.run_date, h.run_time, h.run_status, h.message\n"
                        "FROM msdb.dbo.sysjobhistory h\n"
                        "JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id\n"
                        "WHERE h.step_id = 0 AND h.run_status <> 1\n"
                        "ORDER BY h.run_date DESC, h.run_time DESC;",
                    ),
                    AdviceStep(
                        "Zinciri başlatmak için bir log yedeği alın.",
                        "BACKUP LOG [<veritabani>] TO DISK = N'<yedek_yolu>' WITH CHECKSUM, COMPRESSION;",
                    ),
                    AdviceStep(
                        "Zincir kopmuşsa (tam yedek yok ya da recovery model değişmiş) önce tam "
                        "yedek alın; log yedeği tam yedek olmadan zincir kurmaz.",
                        "BACKUP DATABASE [<veritabani>] TO DISK = N'<yedek_yolu>' WITH CHECKSUM, COMPRESSION;",
                    ),
                ],
                cautions=[
                    "Log yedeği alındıktan sonra log alanı yeniden kullanılabilir hale gelir "
                    "ama dosya KÜÇÜLMEZ; dosya boyutu ayrı bir konudur ve shrink rutin işlem "
                    "olarak yapılmamalıdır.",
                    "`WITH TRUNCATE_ONLY` / `NO_LOG` kullanmayın: zinciri kırar ve o noktadan "
                    "sonra noktasal geri dönüş imkânsız hale gelir.",
                ],
                estimated_duration="Log yedeği genelde saniyeler-dakikalar sürer.",
                rollback="Yedek alma işlemi veriyi değiştirmez; geri alınacak bir şey yoktur.",
                verification=(
                    "SELECT database_name, MAX(backup_finish_date) AS son_log\n"
                    "FROM msdb.dbo.backupset WHERE type = 'L' GROUP BY database_name;"
                ),
            )
        return Advice(
            title="Tam yedek işini yeniden çalıştırın",
            why=(
                f"Son tam yedek {_fmt_age(age_hours)} önce alınmış; eşik {_fmt_age(limit_hours)}. "
                "Tam yedek geri dönüşün TABANI: log yedekleri ne kadar sık olursa olsun, "
                "üzerine uygulanacak bir tam yedek yoksa hiçbirinden geri dönülemez."
            ),
            steps=[
                AdviceStep(
                    "Yedek işinin son çalışma sonucunu inceleyin.",
                    "SELECT j.name, h.run_date, h.run_time, h.run_status, h.message\n"
                    "FROM msdb.dbo.sysjobhistory h\n"
                    "JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id\n"
                    "WHERE h.step_id = 0 ORDER BY h.run_date DESC, h.run_time DESC;",
                ),
                AdviceStep(
                    "Hedef diskte yer olduğunu doğrulayın (yedek işleri en sık bu yüzden düşer).",
                    "EXEC xp_fixeddrives;",
                ),
                AdviceStep(
                    "Tam yedeği elle alın ve doğrulama ile yazın.",
                    "BACKUP DATABASE [<veritabani>] TO DISK = N'<yedek_yolu>'\n"
                    "  WITH CHECKSUM, COMPRESSION, STATS = 5;",
                ),
            ],
            cautions=[
                "`COPY_ONLY` kullanmayın: fark yedeği tabanını güncellemez ve yedek zincirini "
                "planladığınızdan farklı bırakır (elle alınan tek seferlik yedekler dışında).",
                "Yedek diski üretim veri diskiyle aynı fiziksel birim olmamalı; birim "
                "kaybedildiğinde yedek de kaybolur.",
            ],
            estimated_duration="Veritabanı boyutuna göre dakikalar-saatler.",
            rollback="Yedek alma işlemi veriyi değiştirmez.",
            verification=(
                "SELECT database_name, MAX(backup_finish_date) AS son_tam\n"
                "FROM msdb.dbo.backupset WHERE type = 'D' GROUP BY database_name;"
            ),
        )

    # PostgreSQL
    if kind == "log":
        return Advice(
            title="WAL arşivlemesini yeniden çalışır hale getirin",
            why=(
                f"Son WAL arşivlemesi {_fmt_age(age_hours)} önce; eşik {_fmt_age(limit_hours)}. "
                "Sürekli arşivleme durduğunda noktasal geri dönüş (PITR) penceresi son taban "
                "yedeğin alındığı ana çakılır — aradaki tüm işlemler geri getirilemez. Ayrıca "
                "arşivlenemeyen WAL dosyaları veri dizininde birikerek diski doldurur."
            ),
            steps=[
                AdviceStep(
                    "Arşivleyicinin son hatasını okuyun.",
                    "SELECT archive_mode, archived_count, last_archived_time,\n"
                    "       failed_count, last_failed_wal, last_failed_time\n"
                    "FROM pg_stat_archiver, (SELECT current_setting('archive_mode') AS archive_mode) s;",
                ),
                AdviceStep(
                    "Arşiv komutunu elle çalıştırıp gerçek hatayı görün (hedef dizin dolu mu, "
                    "yetki var mı, uzak depo erişilebiliyor mu).",
                    "SHOW archive_command;",
                ),
                AdviceStep(
                    "Sorun giderildikten sonra arşivleyici sayaçlarını sıfırlayıp ilerlemeyi izleyin.",
                    "SELECT pg_stat_reset_shared('archiver');",
                ),
            ],
            cautions=[
                "`archive_command` başarısız olduğu sürece PostgreSQL WAL dosyasını SİLMEZ — "
                "sorun sürerse pg_wal dizini dolar ve sunucu durur. Disk doluluğunu takip edin.",
                "Arşiv hedefini boşaltmak için WAL dosyalarını elle silmeyin; geri dönüş "
                "zincirini kırar.",
            ],
            estimated_duration="Hatanın kaynağına göre dakikalar.",
            rollback="Sayaç sıfırlama yalnızca istatistikleri etkiler, veriyi değiştirmez.",
            verification="SELECT last_archived_time, failed_count FROM pg_stat_archiver;",
        )
    return Advice(
        title="Taban yedeği yeniden alın",
        why=(
            f"Son taban yedek {_fmt_age(age_hours)} önce alınmış; eşik {_fmt_age(limit_hours)}. "
            "Taban yedek eskidikçe geri dönüş süresi uzar: kurtarma, taban yedeğin üzerine o "
            "tarihten bugüne kadarki tüm WAL'ı yeniden oynatmak demektir. Bir aylık WAL'ı "
            "oynatmak saatler sürebilir ve bu süre doğrudan kesinti süresidir."
        ),
        steps=[
            AdviceStep(
                "Yedek aracının son çalışma sonucunu inceleyin (pgBackRest örneği).",
                "pgbackrest --stanza=<stanza> info",
            ),
            AdviceStep(
                "Zamanlanmış işin (cron/systemd timer) çalıştığını doğrulayın.",
                "systemctl list-timers | grep -i backup",
            ),
            AdviceStep(
                "Yeni bir tam yedek alın.",
                "pgbackrest --stanza=<stanza> --type=full backup",
            ),
        ],
        cautions=[
            "Taban yedek alımı IO yükü üretir; yoğun saat dışına planlayın.",
            "Yedek deposunda yer olduğunu önceden doğrulayın — yarıda kalan yedek, olmayan "
            "yedekle aynı şeydir.",
        ],
        estimated_duration="Veri boyutuna göre dakikalar-saatler.",
        rollback="Yedek alma işlemi üretim verisini değiştirmez.",
        verification="pgbackrest --stanza=<stanza> info --output=json",
    )


def _not_found_advice(engine: str, methods_checked: list[str], errors: dict[str, str]) -> Advice:
    guidance = NOT_FOUND_GUIDANCE.get(engine, "")
    steps = [
        AdviceStep(
            "Yedeklerin gerçekten alınıp alınmadığını doğrulayın — dbace'in görememesi ile "
            "yedeğin olmaması farklı şeyler ve bu ayrımı yalnızca siz yapabilirsiniz."
        )
    ]
    if engine == "postgresql":
        steps.append(
            AdviceStep(
                "Yedekler pgBackRest / Barman / WAL-G ile alınıyorsa instance'ın `agent_url` "
                "seçeneğini tanımlayın; dbace bu araçların durumunu yalnızca host-agent "
                "üzerinden okuyabiliyor."
            )
        )
        steps.append(
            AdviceStep(
                "Sürekli arşivleme kullanılıyorsa açık olduğunu doğrulayın.",
                "SELECT name, setting FROM pg_settings WHERE name IN ('archive_mode', 'archive_command');",
            )
        )
    elif engine == "sqlserver":
        steps.append(
            AdviceStep(
                "Bağlanan login'in msdb yedek geçmişini okuyabildiğini doğrulayın.",
                "USE msdb; GRANT SELECT ON dbo.backupset TO [<login>];\n"
                "GRANT SELECT ON dbo.backupmediafamily TO [<login>];",
            )
        )
        steps.append(
            AdviceStep(
                "Yedek geçmişinin temizlenmiş olup olmadığına bakın.",
                "SELECT MIN(backup_start_date), MAX(backup_start_date), COUNT(*) FROM msdb.dbo.backupset;",
            )
        )
    if errors:
        steps.append(
            AdviceStep(
                "Sondayı engelleyen hatalar: "
                + "; ".join(f"{source_label(k)}: {v}" for k, v in list(errors.items())[:3])
            )
        )
    return Advice(
        title="Yedek kaynağını dbace'in görebileceği hale getirin",
        why=(
            "dbace yedek kaydı bulamadı. Bu, yedeğinizin olmadığı anlamına GELMEZ — kaynağa "
            "erişemediği anlamına da gelebilir ve iki durumun sonucu çok farklı. Ayrım "
            "yapılamadığı sürece kurtarma güvencesi verilemez. " + guidance
        ),
        steps=steps,
        cautions=[
            "Bakılan yöntemler: "
            + (", ".join(source_label(m) for m in methods_checked) or "hiçbiri"),
            "Doğrulanmamış bir yedek, yedek sayılmaz: kaynağı görünür hale getirdikten sonra "
            "geri dönüş tatbikatı da yapın.",
        ],
        verification=(
            "Ayar yapıldıktan sonra bir sonraki yedek sondasında bu bulgunun kaybolması gerekir."
        ),
    )


def _failure_advice(engine: str, record: Any) -> Advice:
    return Advice(
        title="Başarısız yedeği inceleyip yeniden çalıştırın",
        why=(
            "Başarısız bir yedek, alınmamış yedektir. Yedek işinin 'çalışıyor' görünmesi "
            "yeterli değil; sonucu başarılı olmayan her deneme kurtarma penceresinde bir "
            "delik bırakır."
        ),
        steps=[
            AdviceStep(
                "Hatanın kendisini okuyun: "
                + (str(record.error_message or "")[:300] or "kaynak hata mesajı vermedi")
            ),
            AdviceStep(
                "En sık iki sebebi eleyin: hedefte yer kalmaması ve yedek kullanıcısının "
                "hedef dizine yazma yetkisinin olmaması."
            ),
            AdviceStep(
                "Sorun giderildikten sonra yedeği elle alıp zincirin kapandığını doğrulayın.",
                "BACKUP DATABASE [<veritabani>] TO DISK = N'<yedek_yolu>' WITH CHECKSUM;"
                if engine == "sqlserver"
                else "pgbackrest --stanza=<stanza> --type=full backup",
            ),
        ],
        cautions=[
            "Başarısız denemeyi tekrar denemeden önce sebebi bulun; aynı hatayla tekrar "
            "denemek yalnızca yedek penceresini tüketir.",
        ],
        verification=(
            "SELECT TOP 5 database_name, type, backup_finish_date FROM msdb.dbo.backupset "
            "ORDER BY backup_start_date DESC;"
            if engine == "sqlserver"
            else "pgbackrest --stanza=<stanza> info"
        ),
    )


def _duration_advice(engine: str, elapsed: float, baseline: float) -> Advice:
    return Advice(
        title="Uzayan yedeğin sebebini inceleyin",
        why=(
            f"Yedek {_fmt_duration(elapsed)} sürdü; son yedeklerin ortalaması "
            f"{_fmt_duration(baseline)}. Uzayan yedek iki şekilde zarar verir: yedek penceresi "
            "yoğun saate taşar ve üretim yavaşlar; bir sonraki yedek başlamadan bitmezse "
            "yedek sıklığı fiilen düşer."
        ),
        steps=[
            AdviceStep("Veri hacminde ani bir artış olup olmadığını kontrol edin."),
            AdviceStep(
                "Yedek hedefinin (disk, ağ paylaşımı, nesne depolama) yavaşlayıp "
                "yavaşlamadığına bakın — uzayan yedeklerin en sık sebebi hedef tarafıdır."
            ),
            AdviceStep(
                "Sıkıştırma açık değilse açmayı değerlendirin: CPU karşılığında yazılan veri "
                "miktarını düşürür.",
                "BACKUP DATABASE [<veritabani>] TO DISK = N'<yedek_yolu>' WITH COMPRESSION;"
                if engine == "sqlserver"
                else "pgbackrest --stanza=<stanza> --compress-type=lz4 backup",
            ),
        ],
        cautions=[
            "Sıkıştırma CPU kullanır; CPU'su zaten sınırdaki bir sunucuda yedek süresini "
            "kısaltmak yerine üretimi yavaşlatabilir.",
        ],
        verification="Bir sonraki yedeğin süresi ortalamaya dönmeli.",
    )


def _size_advice(direction: str) -> Advice:
    if direction == "shrink":
        why = (
            "Yedek boyutu yarıya inmiş durumda. Bunun masum bir açıklaması olabilir (büyük bir "
            "arşivleme, sıkıştırmanın açılması) ama en sık sebebi YEDEĞİN EKSİK OLMASI: "
            "kapsam dışı kalan bir dosya grubu, filtrelenen bir veritabanı ya da yarıda kalıp "
            "başarılı işaretlenmiş bir iş. Eksik yedek, geri dönüş anında fark edilir ve o an "
            "yapılabilecek bir şey yoktur."
        )
    else:
        why = (
            "Yedek boyutu ani biçimde büyüdü. Kapasite planını ve yedek penceresini doğrudan "
            "etkiler; ayrıca beklenmedik bir veri büyümesinin ilk işareti olabilir."
        )
    return Advice(
        title="Yedek boyutundaki ani değişimi doğrulayın",
        why=why,
        steps=[
            AdviceStep("Değişimin bilinen bir işle (arşivleme, toplu yükleme, veri temizliği) açıklanıp açıklanmadığını teyit edin."),
            AdviceStep("Yedeğin kapsamını kontrol edin: tüm veritabanları/dosya grupları dahil mi?"),
            AdviceStep("Yedeğin geri yüklenebilirliğini doğrulayın — boyut anomalisinin tek kesin testi budur."),
        ],
        cautions=[
            "Geri yükleme testini üretim sunucusuna DEĞİL ayrı bir sunucuya yapın.",
        ],
        verification=(
            "RESTORE VERIFYONLY FROM DISK = N'<yedek_dosyasi>' WITH CHECKSUM;"
        ),
    )


def _archiver_advice(reason: str) -> Advice:
    if reason == "off":
        return Advice(
            title="Sürekli WAL arşivlemesini açın",
            why=(
                "`archive_mode` kapalı. Bu durumda yalnızca taban yedeğin alındığı ana geri "
                "dönülebilir; arada yapılan tüm işlemler bir arıza anında kaybolur. Kurtarma "
                "noktası hedefi (RPO) fiilen 'son taban yedek' kadardır."
            ),
            steps=[
                AdviceStep("Arşiv hedefini hazırlayın (yerel dizin, NFS ya da nesne depolama)."),
                AdviceStep(
                    "Arşivlemeyi açın.",
                    "ALTER SYSTEM SET archive_mode = on;\n"
                    "ALTER SYSTEM SET archive_command = '<arsiv_komutu>';",
                ),
                AdviceStep(
                    "`archive_mode` değişikliği YENİDEN BAŞLATMA gerektirir; `archive_command` "
                    "için reload yeterlidir.",
                    "SELECT pg_reload_conf();",
                ),
            ],
            cautions=[
                "`archive_mode` açıldıktan sonra `archive_command` başarısız olursa WAL "
                "dosyaları silinmez ve pg_wal dizini dolar; komutu açmadan önce test edin.",
                "Yeniden başlatma bakım penceresi gerektirir — Patroni kümesinde önce "
                "replikalarda, sonra switchover ile lider düğümde uygulayın.",
            ],
            estimated_duration="Yapılandırma dakikalar; yeniden başlatma kısa bir kesinti.",
            rollback="ALTER SYSTEM RESET archive_mode; ve yeniden başlatma.",
            verification="SELECT archived_count, last_archived_time FROM pg_stat_archiver;",
        )
    return Advice(
        title="Arşivleme hatasını giderin",
        why=(
            "Arşivleyici son denemesinde hata aldı. Arşivleme durduğu sürece hem noktasal geri "
            "dönüş penceresi ilerlemiyor hem de arşivlenemeyen WAL dosyaları veri dizininde "
            "birikip diski dolduruyor — bu, sunucunun tamamen durmasına kadar gidebilir."
        ),
        steps=[
            AdviceStep(
                "Son hatanın hangi WAL dosyasında olduğunu görün.",
                "SELECT last_failed_wal, last_failed_time, failed_count FROM pg_stat_archiver;",
            ),
            AdviceStep("Arşiv komutunu aynı kullanıcıyla elle çalıştırıp gerçek hatayı okuyun."),
            AdviceStep("pg_wal dizinindeki doluluğu kontrol edin; arşivleme durduysa burası büyür."),
        ],
        cautions=[
            "pg_wal içindeki dosyaları ELLE SİLMEYİN — sunucu kurtarılamaz hale gelebilir.",
        ],
        verification="SELECT failed_count, last_archived_time FROM pg_stat_archiver;",
    )


def _slot_advice(slot_name: str) -> Advice:
    return Advice(
        title="Biriken replikasyon slotunu temizleyin ya da tüketiciyi düzeltin",
        why=(
            f"`{slot_name}` slotu WAL biriktiriyor. Kullanılmayan bir slot WAL'ı SONSUZA KADAR "
            "tutar; PostgreSQL'de en sık görülen 'disk doldu' sebeplerinden biridir ve disk "
            "dolduğunda veritabanı yazma alamaz, yani tam kesinti demektir."
        ),
        steps=[
            AdviceStep(
                "Slotun gerçekten kullanılmadığını doğrulayın (aktif değilse tüketicisi yok).",
                "SELECT slot_name, active, wal_status,\n"
                "       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS birikim\n"
                "FROM pg_replication_slots ORDER BY 4 DESC;",
            ),
            AdviceStep(
                "Slot bir replikaya aitse önce replikayı düzeltin — slotu silmek replikayı "
                "kalıcı olarak kopardır."
            ),
            AdviceStep(
                "Slot gerçekten sahipsizse silin.",
                f"SELECT pg_drop_replication_slot('{slot_name}');",
            ),
        ],
        cautions=[
            "Kullanımdaki bir slotu silmek, bağlı replikayı yeniden kurmayı gerektirir — "
            "silmeden önce `active` alanını ve replikanın durumunu MUTLAKA doğrulayın.",
            "Patroni kümelerinde slotlar Patroni tarafından yönetilebilir; elle silinen slot "
            "yeniden oluşturulabilir. Kalıcı çözüm için Patroni yapılandırmasına bakın.",
        ],
        estimated_duration="Doğrulama dakikalar; silme anlık.",
        rollback="Silinen slot geri alınamaz; yeniden oluşturulması gerekir.",
        verification="SELECT slot_name, active, wal_status FROM pg_replication_slots;",
    )


def _recovery_model_advice(database_name: str) -> Advice:
    return Advice(
        title="FULL recovery model ile log yedeği stratejisini uyumlu hale getirin",
        why=(
            f"`{database_name}` FULL recovery model'de ama log yedeği alınmıyor. Bu "
            "yapılandırma iki yönden zararlı: transaction log dosyası sınırsız büyür ve diski "
            "doldurur; buna karşılık FULL model'in tek getirisi olan noktasal geri dönüş de "
            "log yedeği olmadığı için ZATEN kullanılamaz. Yani maliyeti ödenip faydası "
            "alınmıyor."
        ),
        steps=[
            AdviceStep(
                "Noktasal geri dönüş GEREKİYORSA düzenli log yedeği planlayın (ör. 15 dakikada bir).",
                "BACKUP LOG [<veritabani>] TO DISK = N'<yedek_yolu>' WITH CHECKSUM, COMPRESSION;",
            ),
            AdviceStep(
                "Noktasal geri dönüş GEREKMİYORSA recovery model'i SIMPLE'a alın; log dosyası "
                "büyümesi kendiliğinden durur.",
                "ALTER DATABASE [<veritabani>] SET RECOVERY SIMPLE;",
            ),
            AdviceStep(
                "Mevcut log dosyası çok büyüdüyse bir kereye mahsus küçültün.",
                "DBCC SHRINKFILE (N'<log_dosya_adi>', 1024);",
            ),
        ],
        cautions=[
            "SIMPLE'a geçmek log zincirini KIRAR: o andan itibaren noktasal geri dönüş "
            "yapılamaz. Karar iş gereksinimine göre verilmeli, kolaylık için değil.",
            "Always On availability group'taki veritabanları SIMPLE recovery model'i "
            "DESTEKLEMEZ — orada tek seçenek düzenli log yedeğidir.",
            "Shrink rutin bir işlem değildir; indeks parçalanması üretir ve yalnızca bir kez "
            "yapılmalıdır.",
        ],
        estimated_duration="Yapılandırma dakikalar; shrink dosya boyutuna göre.",
        rollback="ALTER DATABASE [<veritabani>] SET RECOVERY FULL; (zincir yeni tam yedekle başlar)",
        verification=(
            "SELECT name, recovery_model_desc, log_reuse_wait_desc FROM sys.databases "
            "WHERE name = N'<veritabani>';"
        ),
    )


# --- Değerlendirme --------------------------------------------------------------------------


def _detection_is_conclusive(engine: str, probe: Any | None) -> bool:
    """dbace "yedek yok" diyebilecek kadar emin mi?

    Emin olmanın koşulu, YETKİLİ KAYNAĞIN gerçekten okunabilmiş olması:

    * SQL Server'da tek yetkili kaynak `msdb`. Okunabildiyse ve boşsa, gerçekten yedek yok
      demektir (üçüncü parti VDI yedekleri hariç — bu, bulgunun metninde söyleniyor).
    * PostgreSQL'de tek bir yetkili kaynak YOK. Gerçek yedekler harici araçlarla alınıyor ve
      onlar yalnızca host-agent üzerinden görülebiliyor. Agent yoksa dbace kör demektir ve
      "yedek yok" DEMEZ — bu, PostgreSQL tarafında en kolay yapılacak yanlış olurdu.
    """
    if probe is None:
        return False
    errors = probe.errors or {}
    if engine == "sqlserver":
        return str(BackupSource.MSDB) not in errors
    if engine == "postgresql":
        if str(BackupSource.PG_STAT_ARCHIVER) in errors:
            return False
        # "backup_tools" hatası = agent yapılandırılmamış ya da erişilemiyor.
        return "backup_tools" not in errors
    return False


def assess_instance(
    instance: Any,
    records: list[Any],
    probe: Any | None,
    *,
    now: datetime | None = None,
) -> BackupAssessment:
    """Bir veritabanının yedek durumunu değerlendirir.

    `records` en fazla birkaç yüz satır olmalı (çağıran tarafta sınırlanıyor); burada tarih
    sıralaması ve seçim yapılıyor.
    """
    now = now or datetime.now(UTC)
    engine = str(instance.engine)
    options = instance.options or {}
    thresholds = thresholds_for(options)
    sticky_hours = failure_sticky_hours(options)

    probe_errors = dict((probe.errors or {}) if probe is not None else {})
    assessment = BackupAssessment(
        instance_id=instance.id,
        instance_name=instance.name,
        engine=engine,
        conclusive=_detection_is_conclusive(engine, probe),
        detected=False,
        methods_checked=list((probe.methods_checked or []) if probe is not None else []),
        methods_found=list((probe.methods_found or []) if probe is not None else []),
        errors=probe_errors,
    )

    archiver = (probe.archiver if probe is not None else None) or {}
    slots = (probe.slots if probe is not None else None) or []
    recovery_models = (probe.recovery_models if probe is not None else None) or []

    full_rows = _successful(records, FULL_EQUIVALENT_TYPES)
    log_rows = _successful(records, LOG_EQUIVALENT_TYPES)

    assessment.last_full_at = _latest_time(full_rows)
    assessment.last_full_age_hours = _age_hours(assessment.last_full_at, now)

    # PostgreSQL'de log yedeğinin karşılığı sürekli arşivleme; onun "son zamanı" bir kayıt
    # satırında değil `pg_stat_archiver` içinde duruyor.
    archiver_last = None
    if archiver.get("last_archived_time"):
        archiver_last = _parse_iso(archiver["last_archived_time"])
    assessment.last_log_at = max(
        [t for t in (_latest_time(log_rows), archiver_last) if t is not None], default=None
    )
    assessment.last_log_age_hours = _age_hours(assessment.last_log_at, now)

    assessment.running = [
        {
            "backup_type": str(r.backup_type),
            "database_name": r.database_name,
            "started_at": (_as_utc(r.started_at) or now).isoformat(),
            "elapsed_seconds": r.duration_seconds,
        }
        for r in records
        if str(r.status) == str(BackupStatus.RUNNING)
    ]

    # Always On: yedeği hangi düğüm alıyor.
    servers = {
        str((r.detail or {}).get("server_name"))
        for r in records
        if (r.detail or {}).get("server_name")
    }
    assessment.backup_servers = sorted(servers)

    assessment.detected = bool(records) or bool(archiver_last)

    # --- 1. Hiç yedek bulunamadı --------------------------------------------------------
    if not assessment.detected:
        if assessment.conclusive:
            detail = (
                "Yedek kaydı bulunamadı ve dbace yetkili kaynağı okuyabildi — yani bu, "
                "erişim sorunu değil. "
                + (
                    "SQL Server yedek geçmişi (msdb) okunabiliyor ama son 90 günde hiç yedek "
                    "kaydı yok. Yedekler VDI/anlık görüntü kullanan üçüncü parti bir araçla "
                    "alınıyorsa msdb'ye kayıt düşmeyebilir; bu ihtimali doğrulayın."
                    if engine == "sqlserver"
                    else "Ne veritabanının kendi kaynakları ne de host-agent üzerinden "
                    "sorgulanan yedek araçları herhangi bir yedek gösterdi."
                )
            )
            severity = "critical"
            title = "Yedek bulunamadı"
        else:
            detail = (
                "dbace yedek kaydı bulamadı ama kaynağa da tam erişemedi — bu, YEDEK OLMADIĞI "
                "ANLAMINA GELMEZ. Aşağıdaki engel giderilmeden yedek durumu hakkında güvence "
                "verilemez."
            )
            severity = "warning"
            title = "Yedek durumu belirlenemedi"
        assessment.issues.append(
            BackupIssue(
                key="backup_not_found",
                severity=severity,
                title=title,
                detail=detail,
                evidence={
                    "metric": "backup_records",
                    "value": 0,
                    "threshold": 1,
                    "measured_at": (
                        _as_utc(probe.probed_at).isoformat()
                        if probe is not None and probe.probed_at
                        else now.isoformat()
                    ),
                },
                facts=[
                    {
                        "label": "Bakılan yöntemler",
                        "value": ", ".join(source_label(m) for m in assessment.methods_checked)
                        or "hiçbiri",
                        "tone": "neutral",
                    },
                    {
                        "label": "Sonuç",
                        "value": "kaynak okunabildi, kayıt yok"
                        if assessment.conclusive
                        else "kaynağa erişilemedi",
                        "tone": "bad",
                    },
                ],
                advice=_not_found_advice(engine, assessment.methods_checked, probe_errors),
            )
        )
    else:
        # --- 2. Yedek yaşı ---------------------------------------------------------------
        _append_age_issue(
            assessment,
            kind="full",
            age_hours=assessment.last_full_age_hours,
            threshold=thresholds[str(BackupType.FULL)],
            engine=engine,
            now=now,
        )
        # Log/WAL eşiği yalnızca o zincirin İZİ VARSA uygulanıyor: log yedeği hiç almayan bir
        # kurulumda (SIMPLE recovery, arşivleme kapalı) saat başı kritik üretmek gürültüden
        # başka bir şey olmazdı. "Hiç yok" durumu recovery model ve arşiv bulgularının işi.
        if assessment.last_log_at is not None:
            _append_age_issue(
                assessment,
                kind="log",
                age_hours=assessment.last_log_age_hours,
                threshold=thresholds[str(BackupType.LOG)],
                engine=engine,
                now=now,
            )

    # --- 3. Başarısız yedekler -----------------------------------------------------------
    for failure in _current_failures(records, now, sticky_hours):
        assessment.issues.append(
            BackupIssue(
                key=f"backup_failed:{failure.backup_type}:{failure.database_name or '-'}",
                severity="critical",
                title=f"{type_label(str(failure.backup_type))} başarısız",
                detail=(
                    f"{type_label(str(failure.backup_type))} "
                    + (f"({failure.database_name}) " if failure.database_name else "")
                    + f"{_fmt_age(_age_hours(failure.started_at, now))} önce başarısız oldu ve "
                    "sonrasında aynı türde başarılı bir yedek alınmadı."
                ),
                evidence={
                    "metric": "backup_status",
                    "value": "failed",
                    "threshold": "success",
                    "measured_at": (_as_utc(failure.started_at) or now).isoformat(),
                },
                facts=[
                    {"label": "Tür", "value": type_label(str(failure.backup_type)), "tone": "neutral"},
                    {"label": "Kaynak", "value": source_label(str(failure.source)), "tone": "neutral"},
                    {
                        "label": "Hata",
                        "value": (str(failure.error_message or "kaynak hata mesajı vermedi"))[:160],
                        "tone": "bad",
                    },
                ],
                advice=_failure_advice(engine, failure),
                database_name=failure.database_name,
            )
        )

    # --- 4. Süre anomalisi ---------------------------------------------------------------
    _append_duration_issue(assessment, records, engine, now)

    # --- 5. Boyut anomalisi --------------------------------------------------------------
    _append_size_issue(assessment, full_rows)

    # --- 6. Arşivleyici sağlığı (PostgreSQL) ---------------------------------------------
    if engine == "postgresql" and archiver:
        _append_archiver_issue(assessment, archiver, now)

    # --- 7. Replikasyon slotu birikimi ---------------------------------------------------
    if engine == "postgresql":
        _append_slot_issues(assessment, slots)

    # --- 8. Recovery model uyumu (SQL Server) --------------------------------------------
    if engine == "sqlserver":
        _append_recovery_model_issues(assessment, recovery_models, now, thresholds)

    # --- Sonuç -----------------------------------------------------------------------------
    worst = assessment.worst_severity()
    if not assessment.detected and not assessment.conclusive:
        assessment.status = "unknown"
        assessment.sla_ok = None
        assessment.summary = "Yedek durumu belirlenemedi."
    else:
        assessment.status = worst if worst != "ok" else "ok"
        # SLA yalnızca YAŞ eşiklerine bakıyor. Süre/boyut anomalisi bir uyarı sinyali ama
        # "yedeğim var mı" sorusunun cevabını değiştirmiyor; ikisini karıştırmak yöneticiye
        # yanlış bir "yedeğiniz yok" mesajı verirdi.
        age_issues = [i for i in assessment.issues if i.key.startswith(("backup_age", "backup_not_found"))]
        assessment.sla_ok = not age_issues
        if assessment.last_full_at is None:
            assessment.summary = "Tam yedek bulunamadı."
        else:
            assessment.summary = (
                f"Son tam yedek {_fmt_age(assessment.last_full_age_hours)} önce."
            )
    return assessment


def _append_age_issue(
    assessment: BackupAssessment,
    *,
    kind: str,
    age_hours: float | None,
    threshold,
    engine: str,
    now: datetime,
) -> None:
    severity = _severity_for_age(age_hours, threshold)
    if severity is None:
        return
    limit = threshold.critical_hours if severity == "critical" else threshold.warning_hours
    label = "Tam yedek" if kind == "full" else ("Log yedeği" if engine == "sqlserver" else "WAL arşivi")
    assessment.issues.append(
        BackupIssue(
            key=f"backup_age:{kind}",
            severity=severity,
            title=f"{label} {_fmt_age(age_hours)} önce alınmış",
            detail=(
                f"{label} yaşı {_fmt_age(age_hours)}; eşik {_fmt_age(limit)}. Bu süre, bir arıza "
                "anında kaybedilecek veri miktarının alt sınırıdır."
            ),
            evidence={
                "metric": f"backup_age_hours_{kind}",
                "value": round(age_hours or 0.0, 2),
                "threshold": limit,
                "measured_at": now.isoformat(),
            },
            facts=[
                {"label": "Yaş", "value": _fmt_age(age_hours), "tone": "bad"},
                {"label": "Uyarı eşiği", "value": _fmt_age(threshold.warning_hours), "tone": "neutral"},
                {"label": "Kritik eşiği", "value": _fmt_age(threshold.critical_hours), "tone": "neutral"},
            ],
            advice=_age_advice(engine, kind, age_hours, limit),
        )
    )


def _current_failures(records: list[Any], now: datetime, sticky_hours: float) -> list[Any]:
    """Hâlâ "güncel" sayılan başarısızlıklar.

    İki koşul birlikte aranıyor:

    1. Başarısızlık `sticky_hours` içinde olmalı — aksi halde altı ay önceki bir hata sonsuza
       kadar kırmızı yanar ve alarm körlüğü yaratır.
    2. Sonrasında aynı türde BAŞARILI bir yedek alınmamış olmalı — yalnızca zamana bakmak,
       15 dakika sonra düzelmiş bir sorunu 24 saat boyunca göstermek demekti.

    Kayıt hiçbir zaman silinmiyor; yalnızca "güncel durum" hesabına girmiyor. Geçmiş, denetim
    için tabloda duruyor.
    """
    failures = [r for r in records if str(r.status) == str(BackupStatus.FAILED)]
    current: list[Any] = []
    for failure in failures:
        started = _as_utc(failure.started_at)
        if started is None or (now - started).total_seconds() / 3600.0 > sticky_hours:
            continue
        later_success = any(
            str(r.status) == str(BackupStatus.SUCCESS)
            and str(r.backup_type) == str(failure.backup_type)
            and (r.database_name or None) == (failure.database_name or None)
            and (_as_utc(r.started_at) or datetime.min.replace(tzinfo=UTC)) > started
            for r in records
        )
        if not later_success:
            current.append(failure)
    current.sort(key=lambda r: _as_utc(r.started_at) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return current[:3]


def _append_duration_issue(
    assessment: BackupAssessment, records: list[Any], engine: str, now: datetime
) -> None:
    """Devam eden ya da en son biten yedeğin süresi anormal mi.

    ORAN VE STATİK TABAN BİRLİKTE aranıyor. Yalnızca orana bakmak, süreler küçükken sürekli
    yanlış alarm üretir: 4 saniyelik bir yedeğin 9 saniyeye çıkması "2 kat yavaşladı" ama
    kimse bunun için uyandırılmamalı. Yalnızca statik tabana bakmak ise büyük veritabanlarında
    normal olan uzun süreleri sürekli alarma çevirirdi.
    """
    candidates = [
        r
        for r in records
        if str(r.status) in (str(BackupStatus.RUNNING), str(BackupStatus.SUCCESS))
        and r.duration_seconds is not None
    ]
    if not candidates:
        return
    candidates.sort(key=lambda r: _as_utc(r.started_at) or datetime.min.replace(tzinfo=UTC), reverse=True)
    subject = candidates[0]
    baseline_rows = [
        r
        for r in candidates[1:]
        if str(r.status) == str(BackupStatus.SUCCESS)
        and str(r.backup_type) == str(subject.backup_type)
        and (r.duration_seconds or 0) > 0
    ][:DURATION_BASELINE_SAMPLES]

    elapsed = float(subject.duration_seconds or 0.0)
    if len(baseline_rows) < DURATION_BASELINE_MIN_SAMPLES:
        # Kanıt yetersiz: karşılaştıracak geçmiş yok, bulgu ÜRETİLMİYOR. Tek bir önceki
        # yedeğe göre "iki kat yavaşladı" demek, ölçüm değil tahmin olurdu.
        return
    baseline = sum(float(r.duration_seconds) for r in baseline_rows) / len(baseline_rows)
    if elapsed < DURATION_ANOMALY_MIN_SECONDS or elapsed < baseline * DURATION_ANOMALY_MULTIPLIER:
        return
    running = str(subject.status) == str(BackupStatus.RUNNING)
    assessment.issues.append(
        BackupIssue(
            key=f"backup_duration:{subject.backup_type}",
            severity="warning",
            title=(
                f"{type_label(str(subject.backup_type))} beklenenden uzun sürüyor"
                if running
                else f"{type_label(str(subject.backup_type))} beklenenden uzun sürdü"
            ),
            detail=(
                f"Süre {_fmt_duration(elapsed)}; son {len(baseline_rows)} başarılı yedeğin "
                f"ortalaması {_fmt_duration(baseline)}. Eşik hem oran ({DURATION_ANOMALY_MULTIPLIER:g}×) "
                f"hem de statik taban ({_fmt_duration(DURATION_ANOMALY_MIN_SECONDS)}) birlikte "
                "sağlandığı için bildiriliyor."
            ),
            evidence={
                "metric": "backup_duration_seconds",
                "value": round(elapsed, 1),
                "threshold": round(max(baseline * DURATION_ANOMALY_MULTIPLIER, DURATION_ANOMALY_MIN_SECONDS), 1),
                "measured_at": (_as_utc(subject.started_at) or now).isoformat(),
            },
            facts=[
                {"label": "Süre", "value": _fmt_duration(elapsed), "tone": "bad"},
                {"label": "Ortalama", "value": _fmt_duration(baseline), "tone": "neutral"},
                {"label": "Durum", "value": "devam ediyor" if running else "tamamlandı", "tone": "neutral"},
            ],
            advice=_duration_advice(engine, elapsed, baseline),
            database_name=subject.database_name,
        )
    )


def _append_size_issue(assessment: BackupAssessment, full_rows: list[Any]) -> None:
    sized = [r for r in full_rows if (r.size_bytes or 0) > 0]
    if len(sized) < DURATION_BASELINE_MIN_SAMPLES + 1:
        return
    latest = sized[0]
    baseline_rows = sized[1 : 1 + DURATION_BASELINE_SAMPLES]
    baseline = sum(float(r.size_bytes) for r in baseline_rows) / len(baseline_rows)
    if baseline <= 0:
        return
    ratio = (float(latest.size_bytes) - baseline) / baseline
    if abs(ratio) < SIZE_ANOMALY_RATIO:
        return
    direction = "grow" if ratio > 0 else "shrink"
    assessment.issues.append(
        BackupIssue(
            key=f"backup_size:{direction}",
            # Küçülme daha ciddi: en sık sebebi eksik yedektir ve eksik yedek, geri dönüş
            # anında fark edilir — yani hiçbir şey yapılamayacak anda.
            severity="warning" if direction == "grow" else "critical",
            title=(
                "Yedek boyutu ani biçimde büyüdü"
                if direction == "grow"
                else "Yedek boyutu ani biçimde küçüldü"
            ),
            detail=(
                f"Son yedek {_fmt_bytes(float(latest.size_bytes))}; önceki "
                f"{len(baseline_rows)} yedeğin ortalaması {_fmt_bytes(baseline)} "
                f"(%{abs(ratio) * 100:.0f} değişim)."
            ),
            evidence={
                "metric": "backup_size_bytes",
                "value": float(latest.size_bytes),
                "threshold": round(baseline * (1 + SIZE_ANOMALY_RATIO if direction == "grow" else 1 - SIZE_ANOMALY_RATIO), 1),
                "measured_at": (_as_utc(latest.started_at) or datetime.now(UTC)).isoformat(),
            },
            facts=[
                {"label": "Son yedek", "value": _fmt_bytes(float(latest.size_bytes)), "tone": "bad"},
                {"label": "Ortalama", "value": _fmt_bytes(baseline), "tone": "neutral"},
                {"label": "Değişim", "value": f"%{ratio * 100:+.0f}", "tone": "bad"},
            ],
            advice=_size_advice(direction),
            database_name=latest.database_name,
        )
    )


def _append_archiver_issue(assessment: BackupAssessment, archiver: dict, now: datetime) -> None:
    mode = str(archiver.get("archive_mode") or "off").lower()
    if mode in ("off", "false", "0"):
        assessment.issues.append(
            BackupIssue(
                key="wal_archiving_off",
                severity="warning",
                title="Sürekli WAL arşivlemesi kapalı",
                detail=(
                    "`archive_mode` kapalı. Yalnızca taban yedeğin alındığı ana geri dönülebilir; "
                    "aradaki işlemler bir arıza anında kurtarılamaz. Yedekleme stratejisi bilinçli "
                    "olarak yalnızca anlık görüntüye dayanıyorsa bu bulgu yoksayılabilir."
                ),
                evidence={
                    "metric": "archive_mode",
                    "value": mode,
                    "threshold": "on",
                    "measured_at": now.isoformat(),
                },
                facts=[{"label": "archive_mode", "value": mode, "tone": "bad"}],
                advice=_archiver_advice("off"),
            )
        )
        return

    last_failed = _parse_iso(archiver.get("last_failed_time"))
    last_archived = _parse_iso(archiver.get("last_archived_time"))
    failed_count = int(archiver.get("failed_count") or 0)
    # SAYAÇ DEĞİL ZAMAN DAMGASI belirleyici: `failed_count` istatistik sıfırlanana kadar
    # birikir; geçmişte bir kez hata almış olmak sorun demek değil. Sorun, SON hatanın son
    # başarılı arşivlemeden SONRA olması — o zaman arşivleme şu anda çalışmıyor demektir.
    if failed_count > 0 and last_failed is not None and (
        last_archived is None or last_failed > last_archived
    ):
        assessment.issues.append(
            BackupIssue(
                key="wal_archiving_failing",
                severity="critical",
                title="WAL arşivlemesi şu anda başarısız",
                detail=(
                    f"Son arşivleme hatası {_fmt_age(_age_hours(last_failed, now))} önce ve bu "
                    "hatadan sonra başarılı bir arşivleme yapılmadı. Arşivleme durduğu sürece "
                    "hem geri dönüş penceresi ilerlemiyor hem de WAL dosyaları veri dizininde "
                    "birikerek diski dolduruyor."
                ),
                evidence={
                    "metric": "archiver_failed_count",
                    "value": failed_count,
                    "threshold": 0,
                    "measured_at": last_failed.isoformat(),
                },
                facts=[
                    {"label": "Son hata", "value": _fmt_age(_age_hours(last_failed, now)) + " önce", "tone": "bad"},
                    {
                        "label": "Son başarılı arşivleme",
                        "value": (_fmt_age(_age_hours(last_archived, now)) + " önce") if last_archived else "hiç",
                        "tone": "bad",
                    },
                    {"label": "Hatalı deneme", "value": str(failed_count), "tone": "bad"},
                ],
                advice=_archiver_advice("failing"),
            )
        )


def _append_slot_issues(assessment: BackupAssessment, slots: list) -> None:
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        retained = slot.get("retained_bytes")
        wal_status = str(slot.get("wal_status") or "").lower()
        name = str(slot.get("slot_name") or "?")
        severity: str | None = None
        if wal_status == "lost":
            # `lost` = gereken WAL zaten silinmiş; slot artık işe yaramaz ve ona güvenen
            # replika yeniden kurulmalı. Birikim eşiğinden bağımsız olarak kritik.
            severity = "critical"
            detail = (
                f"`{name}` slotunun ihtiyaç duyduğu WAL dosyaları silinmiş (wal_status = lost). "
                "Bu slota bağlı replika ya da mantıksal abone artık devam edemez; yeniden "
                "kurulması gerekir."
            )
        elif isinstance(retained, (int, float)) and retained >= SLOT_RETAINED_WAL_CRITICAL_BYTES:
            severity = "critical"
            detail = (
                f"`{name}` slotu {_fmt_bytes(float(retained))} WAL biriktirdi. Disk dolarsa "
                "veritabanı yazma alamaz — bu, tam kesinti demektir."
            )
        elif isinstance(retained, (int, float)) and retained >= SLOT_RETAINED_WAL_WARNING_BYTES:
            severity = "warning"
            detail = (
                f"`{name}` slotu {_fmt_bytes(float(retained))} WAL biriktirdi ve birikim "
                "sürüyorsa disk dolacaktır."
            )
        if severity is None:
            continue
        assessment.issues.append(
            BackupIssue(
                key=f"replication_slot:{name}",
                severity=severity,
                title=f"Replikasyon slotu WAL biriktiriyor: {name}",
                detail=detail,
                evidence={
                    "metric": "slot_retained_bytes",
                    "value": float(retained) if isinstance(retained, (int, float)) else 0.0,
                    "threshold": SLOT_RETAINED_WAL_WARNING_BYTES,
                    "measured_at": datetime.now(UTC).isoformat(),
                },
                facts=[
                    {"label": "Slot", "value": name, "tone": "neutral"},
                    {
                        "label": "Birikim",
                        "value": _fmt_bytes(float(retained)) if isinstance(retained, (int, float)) else "bilinmiyor",
                        "tone": "bad",
                    },
                    {
                        "label": "Aktif",
                        "value": "evet" if slot.get("active") else "hayır",
                        "tone": "neutral" if slot.get("active") else "bad",
                    },
                ],
                advice=_slot_advice(name),
            )
        )


def _append_recovery_model_issues(
    assessment: BackupAssessment, recovery_models: list, now: datetime, thresholds: dict
) -> None:
    """FULL recovery model + log yedeği yok = kaçınılabilir bir disk dolma sebebi."""
    log_threshold = thresholds[str(BackupType.LOG)]
    for row in recovery_models:
        if not isinstance(row, dict):
            continue
        if str(row.get("recovery_model") or "").upper() != "FULL":
            continue
        last_log = _parse_iso(row.get("last_log_at"))
        age = _age_hours(last_log, now)
        if last_log is not None and (age or 0) < log_threshold.critical_hours:
            continue
        database_name = str(row.get("database_name") or "?")
        assessment.issues.append(
            BackupIssue(
                key=f"recovery_model_mismatch:{database_name}",
                severity="warning",
                title=f"FULL recovery model ama log yedeği yok: {database_name}",
                detail=(
                    f"`{database_name}` FULL recovery model'de ancak son log yedeği "
                    + (f"{_fmt_age(age)} önce alınmış." if last_log else "hiç alınmamış.")
                    + " Bu yapılandırmada transaction log dosyası sınırsız büyür; buna karşılık "
                    "FULL model'in tek getirisi olan noktasal geri dönüş de log yedeği "
                    "olmadığı için kullanılamaz."
                ),
                evidence={
                    "metric": "log_backup_age_hours",
                    "value": round(age, 2) if age is not None else None,
                    "threshold": log_threshold.critical_hours,
                    "measured_at": now.isoformat(),
                },
                facts=[
                    {"label": "Veritabanı", "value": database_name, "tone": "neutral"},
                    {"label": "Recovery model", "value": "FULL", "tone": "neutral"},
                    {
                        "label": "Son log yedeği",
                        "value": (_fmt_age(age) + " önce") if last_log else "hiç",
                        "tone": "bad",
                    },
                ],
                advice=_recovery_model_advice(database_name),
                database_name=database_name,
            )
        )


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)
