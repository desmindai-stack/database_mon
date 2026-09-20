"""SQL Server Query Store: plan geçmişi ve plan regresyonu (Faz 31 Commit 9, madde 4).

**Neden:** SQL Server'da bir sorgunun planı değişebilir (istatistik güncellemesi, parametre duyarlılığı,
index değişikliği) ve yeni plan ESKİSİNDEN YAVAŞ olabilir. DMV'ler yalnızca ŞU ANKİ planı gösterir; "dün
hızlıydı, bugün yavaş" sorusu ancak plan GEÇMİŞİYLE cevaplanır. Query Store bu geçmişi tutuyor.

**Yetki:** `sys.query_store_*` katalog görünümleri izlenen veritabanında VIEW DATABASE STATE ister —
bankadaki salt-okunur login'in sahip olduğu yetki (deploy/onprem/sql/sqlserver-monitor-login.sql). Yazma
ya da sysadmin gerekmiyor; Query Store'u AÇMAK DBA'nın işi (uygulama açmıyor, komutu söylüyor).

**Sessiz boş ekran yok:** Query Store kapalıysa, sürüm desteklemiyorsa ya da yetki yetmiyorsa sonuç
"ölçülemedi" + gerekçe + gereken ayar/yetki (komut olarak) döner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Regresyon sayılması için yeni planın eskisine göre en az bu kat yavaş olması gerekiyor.
REGRESSION_FACTOR = 1.5
#: Gürültü eşiği: bu kadar milisaniyenin altındaki ortalamalarda kat farkı anlamlı değil (1 ms'nin altında
#: ölçüm çözünürlüğü ve planlama gürültüsü baskın).
MIN_AVG_DURATION_MS = 1.0
#: Bir planın karşılaştırmaya girmesi için en az çalıştırma sayısı.
MIN_EXECUTIONS = 5

STATE_OFF = "off"
KIND_NOT_SQLSERVER = "not_sqlserver"
KIND_DISABLED = "query_store_disabled"
KIND_UNAUTHORIZED = "unauthorized"
KIND_UNSUPPORTED = "unsupported"
KIND_NOT_MEASURED = "not_measured"
KIND_NO_HISTORY = "no_history"

#: Query Store'u AÇMA komutu — DBA çalıştırır (uygulama yazmıyor).
ENABLE_COMMAND = (
    "ALTER DATABASE [{database}] SET QUERY_STORE = ON;\n"
    "ALTER DATABASE [{database}] SET QUERY_STORE (OPERATION_MODE = READ_WRITE, "
    "DATA_FLUSH_INTERVAL_SECONDS = 900, INTERVAL_LENGTH_MINUTES = 60);"
)
GRANT_COMMAND = "USE [{database}]; GRANT VIEW DATABASE STATE TO [{login}];"

#: Plan geçmişi: sorgu × plan kırılımında çalışma istatistikleri. Yalnızca OKUMA.
#: `sys.query_store_runtime_stats` mikrosaniye tutuyor; milisaniyeye burada çevriliyor.
PLAN_HISTORY_SQL = """
SELECT TOP {limit}
    q.query_text_id,
    MIN(q.query_id) AS query_id,
    p.plan_id,
    p.is_forced_plan,
    MIN(rsi.start_time) AS first_seen,
    MAX(rsi.end_time) AS last_seen,
    SUM(rs.count_executions) AS executions,
    SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS avg_duration_ms,
    SUM(rs.avg_cpu_time * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS avg_cpu_ms,
    SUM(rs.avg_logical_io_reads * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) AS avg_logical_reads,
    MAX(qt.query_sql_text) AS query_text
FROM sys.query_store_query AS q
JOIN sys.query_store_query_text AS qt ON qt.query_text_id = q.query_text_id
JOIN sys.query_store_plan AS p ON p.query_id = q.query_id
JOIN sys.query_store_runtime_stats AS rs ON rs.plan_id = p.plan_id
JOIN sys.query_store_runtime_stats_interval AS rsi ON rsi.runtime_stats_interval_id = rs.runtime_stats_interval_id
WHERE rsi.end_time >= DATEADD(hour, -{hours}, SYSUTCDATETIME())
GROUP BY q.query_text_id, p.plan_id, p.is_forced_plan
ORDER BY SUM(rs.avg_duration * rs.count_executions) DESC
"""

STATE_SQL = "SELECT actual_state_desc, readonly_reason FROM sys.database_query_store_options"


@dataclass
class PlanPoint:
    plan_id: int
    executions: int
    avg_duration_ms: float
    avg_cpu_ms: float | None
    avg_logical_reads: float | None
    first_seen: datetime | None
    last_seen: datetime | None
    is_forced: bool


@dataclass
class PlanRegression:
    query_id: int
    query_text: str
    current: PlanPoint
    baseline: PlanPoint
    slowdown_factor: float

    @property
    def is_regression(self) -> bool:
        return self.slowdown_factor >= REGRESSION_FACTOR


@dataclass
class QueryStoreReport:
    state: str | None = None
    queries_with_history: int = 0
    plans: int = 0
    regressions: list[PlanRegression] = field(default_factory=list)
    unavailable_kind: str | None = None
    unavailable_reason: str | None = None
    required_setting: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def detect_regressions(rows: list[dict[str, Any]]) -> tuple[list[PlanRegression], int, int]:
    """Aynı sorgunun planlarını karşılaştırır: EN SON kullanılan plan, daha önceki EN İYİ plandan
    `REGRESSION_FACTOR` kat yavaşsa regresyon.

    Karşılaştırma plan başına ORTALAMA süreye göre; tek çalıştırmalık gürültü elenmesi için en az
    `MIN_EXECUTIONS` çalıştırma ve `MIN_AVG_DURATION_MS` ortalama aranıyor. Plan "zorlanmışsa"
    (is_forced_plan) yine raporlanıyor — zorlanan planın kötüye gitmesi DBA'nın bilmesi gereken bir şey.
    """
    # SORGU METNİNE göre grupluyoruz, query_id'ye göre değil: Query Store aynı metni farklı "context
    # settings" (ör. MAXDOP değişikliği) altında AYRI query_id ile tutuyor — oysa DBA için bu aynı sorgudur
    # ve plan değişimi tam da böyle durumlarda oluyor (gerçek SQL Server'da ölçüldü).
    by_query: dict[int, list[PlanPoint]] = {}
    texts: dict[int, str] = {}
    query_ids: dict[int, int] = {}
    for row in rows:
        text_id = int(row["query_text_id"])
        texts.setdefault(text_id, str(row.get("query_text") or ""))
        query_ids.setdefault(text_id, int(row.get("query_id") or text_id))
        by_query.setdefault(text_id, []).append(
            PlanPoint(
                plan_id=int(row["plan_id"]),
                executions=int(row.get("executions") or 0),
                avg_duration_ms=float(row.get("avg_duration_ms") or 0.0),
                avg_cpu_ms=float(row["avg_cpu_ms"]) if row.get("avg_cpu_ms") is not None else None,
                avg_logical_reads=float(row["avg_logical_reads"]) if row.get("avg_logical_reads") is not None else None,
                first_seen=row.get("first_seen"),
                last_seen=row.get("last_seen"),
                is_forced=bool(row.get("is_forced_plan")),
            )
        )

    regressions: list[PlanRegression] = []
    with_history = 0
    plan_count = 0
    for text_id, points in by_query.items():
        plan_count += len(points)
        usable = [p for p in points if p.executions >= MIN_EXECUTIONS and p.avg_duration_ms >= MIN_AVG_DURATION_MS]
        if len(usable) < 2:
            continue
        with_history += 1
        # En son kullanılan plan "şimdiki" plan. Eşitlikte plan_id: Query Store yeni planı daha büyük id ile
        # yazıyor ve iki plan AYNI istatistik aralığına düşebiliyor (gerçek sunucuda ölçüldü).
        usable.sort(key=lambda p: ((p.last_seen or datetime.min.replace(tzinfo=UTC)), p.plan_id))
        current = usable[-1]
        earlier = usable[:-1]
        baseline = min(earlier, key=lambda p: p.avg_duration_ms)
        if baseline.avg_duration_ms <= 0:
            continue
        factor = current.avg_duration_ms / baseline.avg_duration_ms
        regression = PlanRegression(query_id=query_ids.get(text_id, text_id), query_text=texts.get(text_id, ""),
                                    current=current, baseline=baseline, slowdown_factor=round(factor, 2))
        if regression.is_regression:
            regressions.append(regression)
    regressions.sort(key=lambda r: r.slowdown_factor, reverse=True)
    return regressions, with_history, plan_count


def _unauthorized(error: str) -> bool:
    text = error.lower()
    return "permission" in text or "(297)" in text or "(300)" in text or "principal" in text


def _unsupported(error: str) -> bool:
    text = error.lower()
    return "invalid object name" in text or "(208)" in text


async def build_report(instance, *, hours: int = 168, limit: int = 200, collector=None) -> QueryStoreReport:
    """Plan geçmişi + regresyon raporu. Okunamıyorsa NEDEN okunamadığını döner."""
    from app.collectors.registry import get_collector
    from app.domain.engines import DatabaseEngine
    from app.services.collection import connection_target_for
    from app.services.query_text_privacy import sanitize_stored_query

    report = QueryStoreReport()
    if instance.engine != str(DatabaseEngine.SQLSERVER):
        report.unavailable_kind = KIND_NOT_SQLSERVER
        report.unavailable_reason = (
            "Query Store yalnızca SQL Server'da var. PostgreSQL'de plan geçmişi auto_explain ile "
            "yakalanan planlardan okunuyor (Sorgular sekmesi)."
        )
        return report

    collector = collector or get_collector(DatabaseEngine(instance.engine), connection_target_for(instance))
    try:
        state_rows = await collector.run_readonly(STATE_SQL)
    except Exception as exc:  # noqa: BLE001 — sebebi kullanıcıya yazılıyor
        message = str(exc)
        report.unavailable_kind = KIND_UNAUTHORIZED if _unauthorized(message) else (
            KIND_UNSUPPORTED if _unsupported(message) else KIND_NOT_MEASURED)
        if report.unavailable_kind == KIND_UNAUTHORIZED:
            report.unavailable_reason = (
                "Ölçülemedi: izleme kullanıcısının Query Store görünümlerini okuma yetkisi yok."
            )
            report.required_setting = GRANT_COMMAND.format(database=instance.database, login=instance.username)
        elif report.unavailable_kind == KIND_UNSUPPORTED:
            report.unavailable_reason = (
                "Ölçülemedi: bu sunucuda Query Store görünümleri yok (SQL Server 2016+ gerekiyor)."
            )
        else:
            report.unavailable_reason = f"Ölçülemedi: Query Store durumu okunamadı — {message.splitlines()[0][:300]}"
        return report

    state = str(state_rows[0]["actual_state_desc"]).lower() if state_rows else STATE_OFF
    report.state = state
    if state != "read_write" and state != "read_only":
        report.unavailable_kind = KIND_DISABLED
        report.unavailable_reason = (
            f"Ölçülemedi: Query Store bu veritabanında KAPALI (durum: {state or 'off'}). Plan geçmişi ve plan "
            "regresyonu yalnızca Query Store açıkken görülebilir; DMV'ler yalnızca ŞU ANKİ planı tutuyor ve "
            "sunucu yeniden başlayınca sıfırlanıyor. 'Veri yok' değil, ölçüm yok."
        )
        report.required_setting = ENABLE_COMMAND.format(database=instance.database)
        return report

    try:
        rows = await collector.run_readonly(PLAN_HISTORY_SQL.format(limit=int(limit), hours=int(hours)))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        report.unavailable_kind = KIND_UNAUTHORIZED if _unauthorized(message) else KIND_NOT_MEASURED
        report.unavailable_reason = (
            "Ölçülemedi: Query Store açık ama plan geçmişi okunamadı — " + message.splitlines()[0][:300]
        )
        if report.unavailable_kind == KIND_UNAUTHORIZED:
            report.required_setting = GRANT_COMMAND.format(database=instance.database, login=instance.username)
        return report

    regressions, with_history, plans = detect_regressions(rows)
    for regression in regressions:
        regression.query_text = sanitize_stored_query(regression.query_text, keep_values=False) or ""
    report.regressions = regressions
    report.queries_with_history = with_history
    report.plans = plans
    if not plans:
        report.unavailable_kind = KIND_NO_HISTORY
        report.unavailable_reason = (
            f"Query Store açık ({state}) ama son {hours} saatte kayıt yok. Yeni açılmış olabilir ya da bu "
            "veritabanında sorgu çalışmamış olabilir — 'plan regresyonu yok' anlamına GELMEZ."
        )
    elif not with_history:
        report.unavailable_kind = KIND_NO_HISTORY
        report.unavailable_reason = (
            f"Query Store açık ({state}); {plans} plan kaydı var ama hiçbir sorgunun karşılaştırılabilir ikinci "
            f"planı yok (en az {MIN_EXECUTIONS} çalıştırma ve {MIN_AVG_DURATION_MS:.0f} ms ortalama aranıyor). "
            "Plan değişikliği olmadan regresyon ölçülemez."
        )
    return report
