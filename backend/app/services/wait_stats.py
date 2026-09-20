"""SQL Server bekleme istatistikleri: `sys.dm_os_wait_stats` (Faz 31 Commit 9, madde 5).

**Neden:** "Sunucu yavaş" şikâyetinin ilk cevabı "ne bekliyor?" sorusudur. SQL Server bunu kümülatif
sayaçlarda tutuyor; ham hâliyle okunduğunda ilk sıraları HER ZAMAN boştaki arka plan görevleri kapıyor
(dispatcher, lazywriter, log yöneticisi kuyruğu…).

**Filtre listesi ELLE YAZILMIYOR — ölçülüyor.** Hangi bekleme türünün "arka plan gürültüsü" olduğu
motorun kendi verisinden çıkıyor: `sys.dm_exec_session_wait_stats` + `sys.dm_exec_sessions.is_user_process`
ile her bekleme türünün KULLANICI oturumlarına düşen payı okunuyor. Kullanıcı oturumlarında hiç görülmemiş
türler "arka plan" sayılıyor, gizlenmiyor: kaç tanesinin elendiği ve istenirse listesi dönüyor.

**Kümülatif sayaç:** değerler sunucu açılışından beri birikiyor. İki okuma arasındaki FARK anlamlı olan.
Sunucu yeniden başlarsa sayaçlar sıfırlanıyor; bu `sys.dm_os_sys_info.sqlserver_start_time` ile tespit
ediliyor ve fark "hesaplanamadı" diye işaretleniyor — eksi ya da uydurma bir fark gösterilmiyor.

**Yetki:** VIEW SERVER STATE (bankadaki izleme login'inde var). Yetki yoksa "ölçülemedi + gereken yetki".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Bekleme türü başına tek satır: kümülatif sayaçlar + kullanıcı oturumlarına düşen görev sayısı.
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

KIND_NOT_SQLSERVER = "not_sqlserver"
KIND_UNAUTHORIZED = "unauthorized"
KIND_NOT_MEASURED = "not_measured"
GRANT_COMMAND = "GRANT VIEW SERVER STATE TO [{login}];"


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


@dataclass
class WaitStatsReport:
    server_start_time: datetime | None = None
    sampled_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Sunucu açılışından beri (kümülatif) — her zaman doğru.
    totals: list[WaitEntry] = field(default_factory=list)
    #: Bir önceki okumadan bu yana FARK. Sunucu yeniden başladıysa ya da ilk okumaysa boş.
    delta: list[WaitEntry] = field(default_factory=list)
    delta_since: datetime | None = None
    delta_unavailable_reason: str | None = None
    restarted: bool = False
    #: Kullanıcı oturumlarında görülmediği için elenen tür sayısı ve adları.
    filtered_background: int = 0
    background_types: list[str] = field(default_factory=list)
    unavailable_kind: str | None = None
    unavailable_reason: str | None = None
    required_grant: str | None = None


#: instance_id → (sunucu açılış zamanı, {bekleme türü: satır}, okuma anı). Süreç içi; kalıcı değil.
_LAST: dict[int, tuple[datetime | None, dict[str, WaitEntry], datetime]] = {}


def reset_baseline(instance_id: int | None = None) -> None:
    """Testler ve yeniden yapılandırma için: süreç içi taban okumayı unut."""
    if instance_id is None:
        _LAST.clear()
    else:
        _LAST.pop(instance_id, None)


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


async def build_report(instance, *, limit: int = 40, include_background: bool = False,
                       collector=None) -> WaitStatsReport:
    from app.collectors.registry import get_collector
    from app.domain.engines import DatabaseEngine
    from app.services.collection import connection_target_for

    report = WaitStatsReport()
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
        rows = await collector.run_readonly(WAIT_STATS_SQL.format(limit=int(limit)))
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

    start_time = start_rows[0]["sqlserver_start_time"] if start_rows else None
    entries = sorted(_entries(rows), key=lambda e: e.wait_ms, reverse=True)
    background = [e for e in entries if e.is_background]
    visible = entries if include_background else [e for e in entries if not e.is_background]

    previous = _LAST.get(instance.id) if instance.id is not None else None
    if previous is None:
        report.delta_unavailable_reason = (
            "Fark hesaplanamadı: bu veritabanı için ilk okuma. Değerler sunucunun açılışından beri KÜMÜLATİF; "
            "fark için ikinci bir okuma gerekiyor."
        )
    elif previous[0] != start_time:
        report.restarted = True
        report.delta_unavailable_reason = (
            f"Fark hesaplanamadı: SQL Server yeniden başlatılmış (açılış: {start_time}). Kümülatif bekleme "
            "sayaçları sıfırlandı; önceki okumayla fark almak yanlış (eksi) sonuç verirdi."
        )
    else:
        delta = compute_delta(entries, previous[1])
        report.delta = delta if include_background else [e for e in delta if not e.is_background]
        report.delta_since = previous[2]

    if instance.id is not None:
        _LAST[instance.id] = (start_time, {e.wait_type: e for e in entries}, report.sampled_at)

    report.server_start_time = start_time
    report.totals = visible
    report.filtered_background = 0 if include_background else len(background)
    report.background_types = [e.wait_type for e in background]
    return report
