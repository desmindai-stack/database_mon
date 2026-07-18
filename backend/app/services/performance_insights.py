from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PerformanceInsight:
    severity: str  # critical, high, medium, low, info
    category: str  # memory, io, connections, queries, replication, tuning
    title: str
    description: str
    recommendation: str
    metric_value: float | None = None
    metric_unit: str | None = None


def analyze_metrics(metrics: dict[str, Any]) -> list[PerformanceInsight]:
    insights: list[PerformanceInsight] = []

    if not metrics:
        return insights

    cache_hit = float(metrics.get("cache_hit_ratio") or 0)
    if cache_hit < 90:
        insights.append(
            PerformanceInsight(
                severity="critical" if cache_hit < 85 else "high",
                category="memory",
                title="Buffer cache hit ratio düşük",
                description=f"Cache hit ratio % {cache_hit:.1f}. Çok fazla disk okuması yapılıyor.",
                recommendation="shared_buffers değerini artırın; genel kural olarak RAM’in %25’i kadar başlayın.",
                metric_value=cache_hit,
                metric_unit="%",
            )
        )
    elif cache_hit < 95:
        insights.append(
            PerformanceInsight(
                severity="medium",
                category="memory",
                title="Cache hit ratio iyileştirilebilir",
                description=f"Cache hit ratio % {cache_hit:.1f}.",
                recommendation="Sorgu planlarını ve index kullanımını gözden geçirin; work_mem ve shared_buffers ayarlarını kontrol edin.",
                metric_value=cache_hit,
                metric_unit="%",
            )
        )

    conn_util = float(metrics.get("connection_utilization_pct") or 0)
    if conn_util >= 90:
        insights.append(
            PerformanceInsight(
                severity="critical",
                category="connections",
                title="Connection limit kritik seviyede",
                description=f"Bağlantı kullanımı % {conn_util:.1f}. Yeni bağlantılar reddedilebilir.",
                recommendation="Hemen max_connections artırın veya PgBouncer gibi bir connection pooler devreye alın.",
                metric_value=conn_util,
                metric_unit="%",
            )
        )
    elif conn_util >= 75:
        insights.append(
            PerformanceInsight(
                severity="high",
                category="connections",
                title="Bağlantı kullanımı yüksek",
                description=f"Bağlantı kullanımı % {conn_util:.1f}.",
                recommendation="Idle bağlantıları gözden geçirin; application tarafında connection pool boyutunu optimize edin.",
                metric_value=conn_util,
                metric_unit="%",
            )
        )

    temp_files = float(metrics.get("temp_files_per_sec") or 0)
    temp_bytes = float(metrics.get("temp_bytes_per_sec") or 0)
    if temp_files > 1 or temp_bytes > 10 * 1024 * 1024:
        insights.append(
            PerformanceInsight(
                severity="high",
                category="memory",
                title="Disk üzerinde temp kullanımı yüksek",
                description=f"Saniyede {temp_files:.1f} temp file, {temp_bytes / 1024 / 1024:.1f} MB/s temp yazma.",
                recommendation="work_mem değerini artırın; özellikle sıralama (sort) ve hash işlemleri yapan sorguları inceleyin.",
                metric_value=temp_bytes,
                metric_unit="B/s",
            )
        )

    checkpoints_req = float(metrics.get("checkpoints_req") or 0)
    checkpoints_timed = float(metrics.get("checkpoints_timed") or 0)
    total_checkpoints = checkpoints_req + checkpoints_timed
    if total_checkpoints > 0 and checkpoints_req / total_checkpoints > 0.5:
        insights.append(
            PerformanceInsight(
                severity="medium",
                category="io",
                title="Checkpoint istekleri çok fazla",
                description=f"Checkpoints’lerin % {checkpoints_req / total_checkpoints * 100:.0f}’i talep üzerine.",
                recommendation="max_wal_size ve checkpoint_timeout değerlerini artırın; checkpoint_completion_target ayarını kontrol edin.",
                metric_value=checkpoints_req,
                metric_unit="count",
            )
        )

    deadlocks = int(metrics.get("deadlocks") or 0)
    if deadlocks > 0:
        insights.append(
            PerformanceInsight(
                severity="high",
                category="queries",
                title="Deadlock tespit edildi",
                description=f"Toplam {deadlocks} deadlock.",
                recommendation="Deadlock loglarını inceleyin; transaction sırasını ve lock sürelerini optimize edin.",
                metric_value=deadlocks,
                metric_unit="count",
            )
        )

    lag = metrics.get("replication_lag_bytes")
    if lag is not None and float(lag) > 100 * 1024 * 1024:
        insights.append(
            PerformanceInsight(
                severity="high",
                category="replication",
                title="Replication lag yüksek",
                description=f"Replika geride: {float(lag) / 1024 / 1024:.1f} MB.",
                recommendation="Replika IO ve network durumunu kontrol edin; büyük transaction’ları parçalayın.",
                metric_value=float(lag),
                metric_unit="B",
            )
        )

    blks_read = float(metrics.get("blks_read_per_sec") or 0)
    blks_hit = float(metrics.get("blks_hit_per_sec") or 0)
    total_io = blks_read + blks_hit
    if total_io > 0 and blks_read / total_io > 0.1:
        insights.append(
            PerformanceInsight(
                severity="medium",
                category="io",
                title="Disk okuma oranı yüksek",
                description=f"I/O’nun % {blks_read / total_io * 100:.1f}’i diske gitmek zorunda.",
                recommendation="shared_buffers ve effective_cache_size ayarlarını gözden geçirin; eksik index’leri kontrol edin.",
                metric_value=blks_read,
                metric_unit="blocks/s",
            )
        )

    io_reads = float(metrics.get("io_reads_per_sec") or 0)
    io_writes = float(metrics.get("io_writes_per_sec") or 0)
    if io_reads > 1000 or io_writes > 1000:
        insights.append(
            PerformanceInsight(
                severity="medium",
                category="io",
                title="Yoğun I/O aktivitesi",
                description=f"{io_reads:.0f} okuma/s, {io_writes:.0f} yazma/s.",
                recommendation="Disk katmanını ve sorgu planlarını inceleyin; hot table’lar için index optimizasyonu yapın.",
                metric_value=io_reads + io_writes,
                metric_unit="ops/s",
            )
        )

    return sorted(insights, key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(x.severity, 5))
