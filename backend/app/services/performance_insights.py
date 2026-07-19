from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

CATEGORY_LABELS = {
    "memory": "Bellek / Cache",
    "connections": "Bağlantılar",
    "io": "I/O & Checkpoint",
    "queries": "Sorgular",
    "replication": "Replikasyon",
    "tuning": "Genel Tuning",
    "collection": "Veri Toplama",
}


@dataclass
class PerformanceInsight:
    severity: str  # critical, high, medium, low, info
    category: str
    title: str
    description: str
    recommendation: str
    metric_value: float | None = None
    metric_unit: str | None = None
    action: str | None = None  # queries | metrics | alerts | none


@dataclass
class ChecklistItem:
    key: str
    label: str
    status: str  # ok | warn | critical | unknown
    detail: str


@dataclass
class TuningReport:
    health_score: int
    grade: str
    status: str
    collected_at: datetime | None
    insights: list[PerformanceInsight] = field(default_factory=list)
    checklist: list[ChecklistItem] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def _f(metrics: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = metrics.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _i(metrics: dict[str, Any], key: str, default: int = 0) -> int:
    return int(_f(metrics, key, float(default)))


def analyze_metrics(
    metrics: dict[str, Any] | None,
    *,
    slow_queries: list[dict[str, Any]] | None = None,
    collected_at: datetime | None = None,
    now: datetime | None = None,
) -> TuningReport:
    metrics = dict(metrics or {})
    slow_queries = slow_queries or []
    now = now or datetime.now(UTC)
    insights: list[PerformanceInsight] = []
    checklist: list[ChecklistItem] = []

    if collected_at is not None:
        ts = collected_at if collected_at.tzinfo else collected_at.replace(tzinfo=UTC)
        age_min = (now - ts).total_seconds() / 60
        if age_min > 30:
            insights.append(
                PerformanceInsight(
                    severity="critical" if age_min > 120 else "high",
                    category="collection",
                    title="Metrik toplama gecikmiş",
                    description=f"Son örnek {age_min:.0f} dakika önce alındı. Tuning analizi eski verilere dayanıyor olabilir.",
                    recommendation="Railway worker servisini kontrol edin; instance bağlantısı ve collector loglarını inceleyin.",
                    metric_value=age_min,
                    metric_unit="dk",
                    action="metrics",
                )
            )
            checklist.append(
                ChecklistItem(
                    key="collection_freshness",
                    label="Metrik tazeliği",
                    status="critical" if age_min > 120 else "warn",
                    detail=f"Son toplama: {age_min:.0f} dk önce",
                )
            )
        else:
            checklist.append(
                ChecklistItem(
                    key="collection_freshness",
                    label="Metrik tazeliği",
                    status="ok",
                    detail=f"Son toplama: {age_min:.0f} dk önce",
                )
            )
    else:
        insights.append(
            PerformanceInsight(
                severity="high",
                category="collection",
                title="Henüz metrik yok",
                description="Bu instance için toplanmış metrik bulunamadı.",
                recommendation="Instance bağlantısını test edin ve worker’ın çalıştığını doğrulayın.",
                action="metrics",
            )
        )
        checklist.append(
            ChecklistItem(
                key="collection_freshness",
                label="Metrik tazeliği",
                status="unknown",
                detail="Örnek yok",
            )
        )

    cache_hit = _f(metrics, "cache_hit_ratio")
    if metrics:
        if cache_hit < 90:
            insights.append(
                PerformanceInsight(
                    severity="critical" if cache_hit < 85 else "high",
                    category="memory",
                    title="Buffer cache hit ratio düşük",
                    description=f"Cache hit ratio %{cache_hit:.1f}. Disk okuması yüksek; sorgular yavaşlayabilir.",
                    recommendation="shared_buffers’ı artırın (RAM’in ~%25’i), effective_cache_size’ı güncelleyin; eksik index’leri Tuning/Yavaş Sorgular’dan kontrol edin.",
                    metric_value=cache_hit,
                    metric_unit="%",
                    action="queries",
                )
            )
            checklist.append(ChecklistItem("cache_hit", "Cache hit ratio", "critical" if cache_hit < 85 else "warn", f"%{cache_hit:.1f}"))
        elif cache_hit < 95:
            insights.append(
                PerformanceInsight(
                    severity="medium",
                    category="memory",
                    title="Cache hit ratio iyileştirilebilir",
                    description=f"Cache hit ratio %{cache_hit:.1f}.",
                    recommendation="Sık kullanılan sorguların planlarını ve index kullanımını gözden geçirin.",
                    metric_value=cache_hit,
                    metric_unit="%",
                    action="queries",
                )
            )
            checklist.append(ChecklistItem("cache_hit", "Cache hit ratio", "warn", f"%{cache_hit:.1f}"))
        else:
            checklist.append(ChecklistItem("cache_hit", "Cache hit ratio", "ok", f"%{cache_hit:.1f}"))
            insights.append(
                PerformanceInsight(
                    severity="info",
                    category="memory",
                    title="Cache hit ratio iyi",
                    description=f"Buffer cache hit ratio %{cache_hit:.1f}.",
                    recommendation="shared_buffers mevcut yük için yeterli görünüyor.",
                    metric_value=cache_hit,
                    metric_unit="%",
                )
            )

    conn_util = _f(metrics, "connection_utilization_pct")
    active = _i(metrics, "active_connections")
    max_conn = _i(metrics, "max_connections")
    if metrics:
        if conn_util >= 90 or (max_conn > 0 and active / max_conn >= 0.9):
            insights.append(
                PerformanceInsight(
                    severity="critical",
                    category="connections",
                    title="Connection limit kritik",
                    description=f"Bağlantı kullanımı %{conn_util:.1f} ({active}/{max_conn}). Yeni bağlantılar reddedilebilir.",
                    recommendation="PgBouncer/connection pool ekleyin; idle bağlantıları kapatın; gerekirse max_connections’ı kontrollü artırın.",
                    metric_value=conn_util,
                    metric_unit="%",
                    action="metrics",
                )
            )
            checklist.append(ChecklistItem("connections", "Bağlantı kullanımı", "critical", f"%{conn_util:.1f}"))
        elif conn_util >= 75:
            insights.append(
                PerformanceInsight(
                    severity="high",
                    category="connections",
                    title="Bağlantı kullanımı yüksek",
                    description=f"Bağlantı kullanımı %{conn_util:.1f} ({active}/{max_conn}).",
                    recommendation="Uygulama pool boyutunu optimize edin; uzun süren idle-in-transaction oturumlarını bulun.",
                    metric_value=conn_util,
                    metric_unit="%",
                    action="metrics",
                )
            )
            checklist.append(ChecklistItem("connections", "Bağlantı kullanımı", "warn", f"%{conn_util:.1f}"))
        else:
            checklist.append(ChecklistItem("connections", "Bağlantı kullanımı", "ok", f"%{conn_util:.1f}"))
            insights.append(
                PerformanceInsight(
                    severity="info",
                    category="connections",
                    title="Bağlantı kullanımı normal",
                    description=f"Bağlantı kullanımı %{conn_util:.1f} ({active}/{max_conn}).",
                    recommendation="Mevcut pool ve max_connections ayarı yeterli.",
                    metric_value=conn_util,
                    metric_unit="%",
                )
            )

    temp_files = _f(metrics, "temp_files_per_sec")
    temp_bytes = _f(metrics, "temp_bytes_per_sec") or _f(metrics, "temp_bytes")
    if metrics:
        if temp_files > 1 or temp_bytes > 10 * 1024 * 1024:
            insights.append(
                PerformanceInsight(
                    severity="high",
                    category="memory",
                    title="Disk üzerinde temp kullanımı yüksek",
                    description=f"Temp: {temp_files:.1f} file/s, {temp_bytes / 1024 / 1024:.1f} MB/s.",
                    recommendation="work_mem’i artırın; büyük SORT/HASH yapan sorguları Yavaş Sorgular’dan hedefleyin.",
                    metric_value=temp_bytes,
                    metric_unit="B/s",
                    action="queries",
                )
            )
            checklist.append(ChecklistItem("temp", "Temp dosya kullanımı", "warn", f"{temp_bytes / 1024 / 1024:.1f} MB/s"))
        else:
            checklist.append(ChecklistItem("temp", "Temp dosya kullanımı", "ok", "Anormal temp yok"))
            insights.append(
                PerformanceInsight(
                    severity="info",
                    category="memory",
                    title="Temp kullanımı kontrol altında",
                    description="Anormal temp file / temp byte yazımı görülmedi.",
                    recommendation="work_mem mevcut sorgular için yeterli görünüyor.",
                    metric_value=0,
                    metric_unit="B/s",
                )
            )

    checkpoints_req = _f(metrics, "checkpoints_req")
    checkpoints_timed = _f(metrics, "checkpoints_timed")
    total_checkpoints = checkpoints_req + checkpoints_timed
    if total_checkpoints > 0:
        req_ratio = checkpoints_req / total_checkpoints
        if req_ratio > 0.5:
            insights.append(
                PerformanceInsight(
                    severity="medium",
                    category="io",
                    title="Checkpoint istekleri fazla",
                    description=f"Checkpoint’lerin %{req_ratio * 100:.0f}’i talep üzerine (requested).",
                    recommendation="max_wal_size ve checkpoint_timeout’u artırın; checkpoint_completion_target=0.9 deneyin.",
                    metric_value=checkpoints_req,
                    metric_unit="count",
                    action="metrics",
                )
            )
            checklist.append(ChecklistItem("checkpoints", "Checkpoint sağlığı", "warn", f"%{req_ratio * 100:.0f} requested"))
        else:
            checklist.append(ChecklistItem("checkpoints", "Checkpoint sağlığı", "ok", f"%{req_ratio * 100:.0f} requested"))

    deadlocks = _i(metrics, "deadlocks")
    if metrics:
        if deadlocks > 0:
            insights.append(
                PerformanceInsight(
                    severity="high",
                    category="queries",
                    title="Deadlock tespit edildi",
                    description=f"Toplam {deadlocks} deadlock kaydı var.",
                    recommendation="pg_locks / log_lock_waits ile transaction sırasını ve uzun kilitleri inceleyin.",
                    metric_value=float(deadlocks),
                    metric_unit="count",
                    action="queries",
                )
            )
            checklist.append(ChecklistItem("deadlocks", "Deadlock", "critical", f"{deadlocks} adet"))
        else:
            checklist.append(ChecklistItem("deadlocks", "Deadlock", "ok", "Yok"))
            insights.append(
                PerformanceInsight(
                    severity="info",
                    category="queries",
                    title="Deadlock yok",
                    description="Son dönemde deadlock görülmedi.",
                    recommendation="Concurrency kontrolü iyi görünüyor.",
                    metric_value=0,
                    metric_unit="count",
                )
            )

    lag = metrics.get("replication_lag_bytes")
    if lag is not None:
        lag_f = float(lag)
        if lag_f > 100 * 1024 * 1024:
            insights.append(
                PerformanceInsight(
                    severity="high",
                    category="replication",
                    title="Replication lag yüksek",
                    description=f"Replika {lag_f / 1024 / 1024:.1f} MB geride.",
                    recommendation="Replica I/O ve network’ü kontrol edin; büyük transaction’ları parçalayın.",
                    metric_value=lag_f,
                    metric_unit="B",
                    action="metrics",
                )
            )
            checklist.append(ChecklistItem("replication", "Replication lag", "warn", f"{lag_f / 1024 / 1024:.1f} MB"))
        else:
            checklist.append(ChecklistItem("replication", "Replication lag", "ok", f"{lag_f / 1024 / 1024:.1f} MB"))

    blks_read = _f(metrics, "blks_read_per_sec")
    blks_hit = _f(metrics, "blks_hit_per_sec")
    total_io = blks_read + blks_hit
    if total_io > 0 and blks_read / total_io > 0.1:
        insights.append(
            PerformanceInsight(
                severity="medium",
                category="io",
                title="Disk okuma oranı yüksek",
                description=f"I/O’nun %{blks_read / total_io * 100:.1f}’i diskten geliyor.",
                recommendation="Hot table/index’leri ve shared_buffers ayarını gözden geçirin.",
                metric_value=blks_read,
                metric_unit="blocks/s",
                action="queries",
            )
        )
        checklist.append(ChecklistItem("disk_read", "Disk okuma oranı", "warn", f"%{blks_read / total_io * 100:.1f}"))
    elif metrics:
        checklist.append(ChecklistItem("disk_read", "Disk okuma oranı", "ok", "Cache ağırlıklı"))

    io_reads = _f(metrics, "io_reads_per_sec")
    io_writes = _f(metrics, "io_writes_per_sec")
    if io_reads > 1000 or io_writes > 1000:
        insights.append(
            PerformanceInsight(
                severity="medium",
                category="io",
                title="Yoğun I/O aktivitesi",
                description=f"{io_reads:.0f} okuma/s, {io_writes:.0f} yazma/s.",
                recommendation="Disk katmanı ve sorgu planlarını inceleyin; hot table’lar için index optimizasyonu yapın.",
                metric_value=io_reads + io_writes,
                metric_unit="ops/s",
                action="metrics",
            )
        )

    # Slow query derived insights
    if slow_queries:
        heavy = sorted(slow_queries, key=lambda q: float(q.get("total_time_ms") or 0), reverse=True)
        top = heavy[0] if heavy else None
        mean_heavy = [q for q in heavy if float(q.get("mean_time_ms") or 0) >= 50]
        if top and float(top.get("total_time_ms") or 0) > 1000:
            insights.append(
                PerformanceInsight(
                    severity="medium",
                    category="queries",
                    title="Yüksek toplam süreye sahip sorgu var",
                    description=f"En pahalı sorgu toplam {float(top.get('total_time_ms') or 0):.0f} ms, ortalama {float(top.get('mean_time_ms') or 0):.1f} ms.",
                    recommendation="Yavaş Sorgular sekmesinde bu sorgu için index önerisi çalıştırın; EXPLAIN ANALYZE ile planı doğrulayın.",
                    metric_value=float(top.get("total_time_ms") or 0),
                    metric_unit="ms",
                    action="queries",
                )
            )
        if mean_heavy:
            insights.append(
                PerformanceInsight(
                    severity="high" if len(mean_heavy) >= 3 else "medium",
                    category="queries",
                    title=f"{len(mean_heavy)} yavaş ortalama süreli sorgu",
                    description="Ortalama süresi ≥ 50 ms olan sorgular tespit edildi.",
                    recommendation="Bu sorgular için index advice ve plan iyileştirmesi öncelikli aksiyon olmalı.",
                    metric_value=float(len(mean_heavy)),
                    metric_unit="adet",
                    action="queries",
                )
            )
            checklist.append(ChecklistItem("slow_queries", "Yavaş sorgular", "warn", f"{len(mean_heavy)} adet ≥50ms"))
        else:
            checklist.append(ChecklistItem("slow_queries", "Yavaş sorgular", "ok", f"{len(slow_queries)} örnek, kritik yok"))
    else:
        checklist.append(ChecklistItem("slow_queries", "Yavaş sorgular", "unknown", "Örnek yok / pg_stat_statements?"))
        if metrics:
            insights.append(
                PerformanceInsight(
                    severity="info",
                    category="queries",
                    title="Yavaş sorgu örneği yok",
                    description="pg_stat_statements verisi henüz gelmemiş olabilir.",
                    recommendation="Extension’ın yüklü olduğunu ve worker’ın sorgu topladığını doğrulayın.",
                    action="queries",
                )
            )

    issue_insights = [x for x in insights if x.severity in {"critical", "high", "medium"}]
    if not issue_insights and metrics:
        insights.insert(
            0,
            PerformanceInsight(
                severity="info",
                category="tuning",
                title="Genel durum sağlıklı",
                description="Kritik bir performans sorunu tespit edilmedi. Düzenli kontrol için yavaş sorguları ve alarmları izlemeye devam edin.",
                recommendation="Yavaş Sorgular sekmesinden index önerilerini periyodik çalıştırın.",
                action="queries",
            )
        )

    if not insights:
        insights.append(
            PerformanceInsight(
                severity="info",
                category="tuning",
                title="Tuning hazır",
                description="Veri geldikçe burada DBA aksiyon önerileri görünecek.",
                recommendation="Instance bağlantısını ve worker toplamasını doğrulayın.",
                action="metrics",
            )
        )

    insights = sorted(insights, key=lambda x: SEVERITY_RANK.get(x.severity, 5))
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for item in insights:
        summary[item.severity] = summary.get(item.severity, 0) + 1

    # Score: start 100, subtract by severity
    score = 100
    score -= summary.get("critical", 0) * 25
    score -= summary.get("high", 0) * 12
    score -= summary.get("medium", 0) * 6
    score -= summary.get("low", 0) * 2
    score = max(0, min(100, score))

    if score >= 90:
        grade, status = "A", "healthy"
    elif score >= 75:
        grade, status = "B", "healthy"
    elif score >= 60:
        grade, status = "C", "warning"
    elif score >= 40:
        grade, status = "D", "warning"
    else:
        grade, status = "F", "critical"

    return TuningReport(
        health_score=score,
        grade=grade,
        status=status,
        collected_at=collected_at,
        insights=insights,
        checklist=checklist,
        summary=summary,
    )


# Backward-compatible helper used by older call sites / tests
def analyze_metrics_list(metrics: dict[str, Any]) -> list[PerformanceInsight]:
    return analyze_metrics(metrics).insights
