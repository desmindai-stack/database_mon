"""SQL Server bekleme istatistikleri: `sys.dm_os_wait_stats` (Faz 31 Commit 9, madde 5; taban Commit 10c-A).

**Neden:** "Sunucu yavaş" şikâyetinin ilk cevabı "ne bekliyor?" sorusudur. SQL Server bunu kümülatif
sayaçlarda tutuyor; ham hâliyle okunduğunda ilk sıraları HER ZAMAN boştaki arka plan görevleri kapıyor
(dispatcher, lazywriter, log yöneticisi kuyruğu…).

**Filtre listesi ELLE YAZILMIYOR — ölçülüyor.** Hangi bekleme türünün "arka plan gürültüsü" olduğu
motorun kendi verisinden çıkıyor: `sys.dm_exec_session_wait_stats` + `sys.dm_exec_sessions.is_user_process`
ile her bekleme türünün KULLANICI oturumlarına düşen payı okunuyor. Kullanıcı oturumlarında hiç görülmemiş
türler "arka plan" sayılıyor, gizlenmiyor: kaç tanesinin elendiği ve istenirse listesi dönüyor.

**Kümülatif sayaç ve TABAN (Commit 10c-A):** değerler sunucu açılışından beri birikiyor; anlamlı olan iki okuma arasındaki
FARK. Fark, son KAYITLI okumaya göre hesaplanır. Taban eskiden süreç belleğindeydi ve iki şey yanıltıcıydı: dbace yeniden
başlayınca ilk okuma "fark hesaplanamadı" diyordu; çok süreçte ardışık iki istek farklı sürece düşerse fark "o sürecin
son okumasından bu yana" oluyordu, ekranda işaret yok. Şimdi taban PAYLAŞILAN yerde (meta veritabanı,
`wait_stats_baselines`, instance başına tek satır); `WAIT_STATS_BASELINE=process` eski davranışı geri getirir.
Sunucu yeniden başlarsa sayaçlar sıfırlanır; bu `sys.dm_os_sys_info.sqlserver_start_time` ile tespit edilir ve fark
"hesaplanamadı" diye işaretlenir — eksi ya da uydurma bir fark gösterilmiyor.

**Yetki:** VIEW SERVER STATE (bankadaki izleme login'inde var). Yetki yoksa "ölçülemedi + gereken yetki".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Bekleme türü başına tek satır: kümülatif sayaçlar + kullanıcı oturumlarına düşen görev sayısı. TÜM türler okunuyor (~250
#: satır, ihmal edilebilir): taban her türü içermeli ki sonraki okumadaki fark eksik kalmasın; sayfalama yanıtta uygulanıyor.
WAIT_STATS_SQL = """
SELECT TOP {limit}
    ws.wait_type,
    ws.waiting_tasks_count,
    ws.wait_time_ms,
    ws.signal_wait_time_ms,
    ws.max_wait_time_ms,
    COALESCE(u.user_tasks, 0) AS user_tasks
FROM sys.dm_os_wait_stats AS ws
LEFT JOIN (
    SELECT sws.wait_type, SUM(sws.waiting_tasks_count) AS user_tasks
    FROM sys.dm_exec_session_wait_stats AS sws
    JOIN sys.dm_exec_sessions AS s ON s.session_id = sws.session_id AND s.is_user_process = 1
    GROUP BY sws.wait_type
) AS u ON u.wait_type = ws.wait_type
WHERE ws.wait_time_ms > 0
ORDER BY ws.wait_time_ms DESC
"""

START_TIME_SQL = "SELECT sqlserver_start_time FROM sys.dm_os_sys_info"

#: Okunacak en fazla bekleme türü (gerçekte ~250; sınır yalnızca güvenlik).
MAX_WAIT_TYPES = 1000

#: Taban en çok bu sıklıkta yenilenir: ekranı her açan kullanıcı meta veritabanına yazım üretmesin (Commit 10c-A ölçümü:
#: satır başına ~5 KB). Sunucu yeniden başlamışsa bu sınır beklenmez.
BASELINE_MIN_AGE_SECONDS = 60.0

#: Bu kadar eski bir tabana karşı fark "uzun pencere" notuyla gösterilir (kullanıcı kısa pencere sanmasın).
LONG_WINDOW_SECONDS = 24 * 3600.0

KIND_NOT_SQLSERVER = "not_sqlserver"
KIND_UNAUTHORIZED = "unauthorized"
KIND_NOT_MEASURED = "not_measured"
GRANT_COMMAND = "GRANT VIEW SERVER STATE TO [{login}];"

SOURCE_SHARED = "shared"
SOURCE_PROCESS = "process"


@dataclass
class WaitEntry:
    wait_type: str
    waiting_tasks: int
    wait_ms: float
    signal_ms: float
    max_wait_ms: float
    user_tasks: int

    @property
    def is_background(self) -> bool:
        """Kullanıcı oturumlarında HİÇ görülmemiş: arka plan görevi (ölçülen, varsayılan değil)."""
        return self.user_tasks == 0

    @property
    def avg_wait_ms(self) -> float:
        return round(self.wait_ms / self.waiting_tasks, 3) if self.waiting_tasks else 0.0

    def to_counters(self) -> list[float]:
        return [self.waiting_tasks, self.wait_ms, self.signal_ms, self.max_wait_ms, self.user_tasks]

    @classmethod
    def from_counters(cls, wait_type: str, values: list[float]) -> WaitEntry:
        tasks, ms, signal, longest, user = (list(values) + [0] * 5)[:5]
        return cls(wait_type, int(tasks), float(ms), float(signal), float(longest), int(user))


@dataclass
class Baseline:
    """Kayıtlı önceki okuma."""

    server_start_time: datetime | None
    sampled_at: datetime
    entries: dict[str, WaitEntry]
    writer: str | None = None


@dataclass
class WaitStatsReport:
    server_start_time: datetime | None = None
    sampled_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Sunucu açılışından beri (kümülatif) — her zaman doğru.
    totals: list[WaitEntry] = field(default_factory=list)
    #: Kayıtlı önceki okumadan bu yana FARK. Sunucu yeniden başladıysa ya da taban yoksa boş.
    delta: list[WaitEntry] = field(default_factory=list)
    delta_since: datetime | None = None
    delta_window_seconds: float | None = None
    delta_unavailable_reason: str | None = None
    #: Fark hakkında ek not (ör. çok uzun karşılaştırma penceresi); sorun değil, bağlam.
    delta_note: str | None = None
    restarted: bool = False
    #: Tabanın nerede tutulduğu: "shared" (meta veritabanı) | "process" (bu sürecin belleği).
    baseline_source: str = SOURCE_SHARED
    #: Bu okuma tabanı yeniledi mi (en çok dakikada bir).
    baseline_saved: bool = False
    #: Bu okumayı işleyen süreç (teşhis: ardışık isteklerde farklıysa çok süreçli çalışıyorsunuz).
    process_id: int = field(default_factory=os.getpid)
    #: Kullanıcı oturumlarında görülmediği için elenen tür sayısı ve adları.
    filtered_background: int = 0
    background_types: list[str] = field(default_factory=list)
    unavailable_kind: str | None = None
    unavailable_reason: str | None = None
    required_grant: str | None = None


# --- Taban depoları ----------------------------------------------------------------------------


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _writer() -> str:
    return f"pid:{os.getpid()}"


class MemoryBaselineStore:
    """SÜREÇ BELLEĞİ (eski davranış). Yeniden başlatmada ve süreçler arasında kaybolur; `WAIT_STATS_BASELINE=process`."""

    source = SOURCE_PROCESS

    def __init__(self, data: dict[int, Baseline] | None = None) -> None:
        self._data = _MEMORY if data is None else data

    async def load(self, instance_id: int) -> Baseline | None:
        return self._data.get(instance_id)

    async def save(self, instance_id: int, baseline: Baseline) -> None:
        self._data[instance_id] = baseline


class DatabaseBaselineStore:
    """PAYLAŞILAN taban: meta veritabanı, instance başına tek satır."""

    source = SOURCE_SHARED

    def __init__(self, session) -> None:
        self._session = session

    async def load(self, instance_id: int) -> Baseline | None:
        from app.models import WaitStatsBaseline

        row = await self._session.get(WaitStatsBaseline, instance_id)
        if row is None:
            return None
        entries = {name: WaitEntry.from_counters(name, values) for name, values in (row.counters or {}).items()}
        return Baseline(_aware(row.server_start_time), _aware(row.sampled_at), entries, row.writer)

    async def save(self, instance_id: int, baseline: Baseline) -> None:
        from app.models import WaitStatsBaseline

        counters = {name: entry.to_counters() for name, entry in baseline.entries.items()}
        row = await self._session.get(WaitStatsBaseline, instance_id)
        if row is None:
            self._session.add(WaitStatsBaseline(
                instance_id=instance_id, server_start_time=baseline.server_start_time, sampled_at=baseline.sampled_at,
                counters=counters, writer=baseline.writer))
        else:
            row.server_start_time, row.sampled_at, row.counters, row.writer = (
                baseline.server_start_time, baseline.sampled_at, counters, baseline.writer)
        await self._session.commit()


#: Süreç içi (eski) depo verisi: instance_id -> Baseline.
_MEMORY: dict[int, Baseline] = {}


def reset_baseline(instance_id: int | None = None) -> None:
    """Testler ve yeniden yapılandırma için: SÜREÇ İÇİ tabanı unut (paylaşılan tabana dokunmaz)."""
    if instance_id is None:
        _MEMORY.clear()
    else:
        _MEMORY.pop(instance_id, None)


def make_store(session=None):
    """Yapılandırmaya göre taban deposu. Oturum yoksa (araçlar/testler) süreç belleği."""
    from app.config import settings

    if session is not None and settings.wait_stats_baseline == SOURCE_SHARED:
        return DatabaseBaselineStore(session)
    return MemoryBaselineStore()


# --- Hesap ---------------------------------------------------------------------------------------


def _entries(rows: list[dict[str, Any]]) -> list[WaitEntry]:
    return [
        WaitEntry(
            wait_type=str(row["wait_type"]),
            waiting_tasks=int(row.get("waiting_tasks_count") or 0),
            wait_ms=float(row.get("wait_time_ms") or 0),
            signal_ms=float(row.get("signal_wait_time_ms") or 0),
            max_wait_ms=float(row.get("max_wait_time_ms") or 0),
            user_tasks=int(row.get("user_tasks") or 0),
        )
        for row in rows
    ]


def compute_delta(current: list[WaitEntry], previous: dict[str, WaitEntry]) -> list[WaitEntry]:
    """İki kümülatif okuma arasındaki fark. Sayaç düşmüşse (kısmi sıfırlama) o tür atlanıyor."""
    out: list[WaitEntry] = []
    for entry in current:
        before = previous.get(entry.wait_type)
        if before is None:
            out.append(entry)
            continue
        if entry.wait_ms < before.wait_ms or entry.waiting_tasks < before.waiting_tasks:
            continue
        difference = WaitEntry(
            wait_type=entry.wait_type,
            waiting_tasks=entry.waiting_tasks - before.waiting_tasks,
            wait_ms=entry.wait_ms - before.wait_ms,
            signal_ms=max(0.0, entry.signal_ms - before.signal_ms),
            max_wait_ms=entry.max_wait_ms,
            user_tasks=entry.user_tasks,
        )
        if difference.wait_ms > 0 or difference.waiting_tasks > 0:
            out.append(difference)
    out.sort(key=lambda e: e.wait_ms, reverse=True)
    return out


def _unauthorized(message: str) -> bool:
    text = message.lower()
    return "permission" in text or "(297)" in text or "(300)" in text


def _human_window(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} sn"
    if seconds < 5400:
        return f"{seconds / 60:.0f} dk"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} saat"
    return f"{seconds / 86400:.1f} gün"


def first_reading_reason(source: str) -> str:
    """Kayıtlı önceki okuma yokken ekranda görünen gerekçe — yeniden başlatma ve ilk kurulum için."""
    if source == SOURCE_SHARED:
        return (
            "Fark hesaplanamadı: bu veritabanı için kayıtlı önceki okuma yok (ilk okuma). Şimdiki değerler kaydedildi; "
            "sonraki okumada fark gösterilecek. Aşağıdaki değerler sunucunun açılışından beri KÜMÜLATİF."
        )
    return (
        "Fark hesaplanamadı: önceki okuma yok — bu SÜREÇ yeni başlamış ya da bu veritabanı bu süreçte ilk kez okunuyor "
        "(taban süreç belleğinde tutuluyor, WAIT_STATS_BASELINE=process; yeniden başlatmada ve diğer süreçlerle paylaşılmıyor). "
        "Sonraki okumada fark gösterilecek. Aşağıdaki değerler sunucunun açılışından beri KÜMÜLATİF."
    )


async def build_report(instance, *, limit: int = MAX_WAIT_TYPES, include_background: bool = False,
                       collector=None, store=None) -> WaitStatsReport:
    from app.collectors.registry import get_collector
    from app.domain.engines import DatabaseEngine
    from app.services.collection import connection_target_for

    store = store or make_store()
    report = WaitStatsReport(baseline_source=store.source)
    if instance.engine != str(DatabaseEngine.SQLSERVER):
        report.unavailable_kind = KIND_NOT_SQLSERVER
        report.unavailable_reason = (
            "`sys.dm_os_wait_stats` SQL Server'a özgü. PostgreSQL'de bekleme kırılımı veritabanı yükü "
            "(AAS) ekranında, bekleme örnekleyicisinden geliyor."
        )
        return report

    collector = collector or get_collector(DatabaseEngine(instance.engine), connection_target_for(instance))
    try:
        start_rows = await collector.run_readonly(START_TIME_SQL)
        rows = await collector.run_readonly(WAIT_STATS_SQL.format(limit=int(min(limit, MAX_WAIT_TYPES))))
    except Exception as exc:  # noqa: BLE001 — sebebi kullanıcıya yazılıyor
        message = str(exc)
        report.unavailable_kind = KIND_UNAUTHORIZED if _unauthorized(message) else KIND_NOT_MEASURED
        if report.unavailable_kind == KIND_UNAUTHORIZED:
            report.unavailable_reason = (
                "Ölçülemedi: izleme login'inin bekleme istatistiklerini okuma yetkisi yok "
                "(sys.dm_os_wait_stats VIEW SERVER STATE ister)."
            )
            report.required_grant = GRANT_COMMAND.format(login=instance.username)
        else:
            report.unavailable_reason = f"Ölçülemedi: bekleme istatistikleri okunamadı — {message.splitlines()[0][:300]}"
        return report

    now = datetime.now(UTC)
    start_time = _aware(start_rows[0]["sqlserver_start_time"]) if start_rows else None
    entries = sorted(_entries(rows), key=lambda e: e.wait_ms, reverse=True)
    background = [e for e in entries if e.is_background]
    visible = entries if include_background else [e for e in entries if not e.is_background]

    previous: Baseline | None = None
    if instance.id is not None:
        try:
            previous = await store.load(instance.id)
        except Exception as exc:  # noqa: BLE001 — paylaşılan tabana ulaşılamadı: süreç belleğine düş, ama SÖYLE
            logger.warning("wait-stats tabanı okunamadı (instance %s): %s", instance.id, exc)
            store = MemoryBaselineStore()
            report.baseline_source = SOURCE_PROCESS
            previous = await store.load(instance.id)
            report.delta_note = ("Paylaşılan taban (meta veritabanı) okunamadı; bu süreçteki son okumayla karşılaştırıldı — "
                                 "süreçler arası ve yeniden başlatma sonrası fark güvenilir değil.")

    if previous is None:
        report.delta_unavailable_reason = first_reading_reason(report.baseline_source)
    elif _aware(previous.server_start_time) != start_time:
        report.restarted = True
        report.delta_unavailable_reason = (
            f"Fark hesaplanamadı: SQL Server yeniden başlatılmış (açılış: {start_time}). Kümülatif bekleme "
            "sayaçları sıfırlandı; önceki okumayla fark almak yanlış (eksi) sonuç verirdi. Yeni değerler taban olarak kaydedildi."
        )
    else:
        delta = compute_delta(entries, previous.entries)
        report.delta = delta if include_background else [e for e in delta if not e.is_background]
        report.delta_since = previous.sampled_at
        report.delta_window_seconds = max(0.0, (now - previous.sampled_at).total_seconds())
        if report.delta_window_seconds >= LONG_WINDOW_SECONDS:
            report.delta_note = ((report.delta_note + " ") if report.delta_note else "") + (
                f"Karşılaştırma penceresi uzun: {_human_window(report.delta_window_seconds)} (son kayıtlı okumadan bu yana). "
                "Bu, 'şu anda ne bekliyoruz' değil, o aralığın toplamıdır.")

    # Tabanı yenile: yok, sunucu yeniden başlamış ya da yeterince eski ise (en çok dakikada bir).
    age = (now - previous.sampled_at).total_seconds() if previous else None
    if instance.id is not None and (previous is None or report.restarted or (age or 0) >= BASELINE_MIN_AGE_SECONDS):
        try:
            await store.save(instance.id, Baseline(start_time, now, {e.wait_type: e for e in entries}, _writer()))
            report.baseline_saved = True
        except Exception as exc:  # noqa: BLE001 — okuma başarılı; tabanın yazılamaması ekranı düşürmemeli
            logger.warning("wait-stats tabanı yazılamadı (instance %s): %s", instance.id, exc)
            report.delta_note = ((report.delta_note + " ") if report.delta_note else "") + (
                "Taban kaydedilemedi; sonraki okuma bu okumaya göre fark gösteremeyebilir.")

    logger.info("wait-stats okuma: instance=%s pid=%s taban=%s önceki=%s pencere=%s kaydedildi=%s", instance.id, os.getpid(),
                report.baseline_source, previous.sampled_at.isoformat() if previous else None,
                None if report.delta_window_seconds is None else round(report.delta_window_seconds, 1), report.baseline_saved)

    report.server_start_time = start_time
    report.sampled_at = now
    report.totals = visible
    report.filtered_background = 0 if include_background else len(background)
    report.background_types = [e.wait_type for e in background]
    return report
