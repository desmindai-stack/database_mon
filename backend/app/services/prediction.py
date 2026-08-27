from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import mean

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.metrics import METRIC_KEYS
from app.models import MetricSample, PredictionInsight


def _linear_forecast(values: list[float], horizon_points: int) -> tuple[float, float]:
    """Simple least-squares slope extrapolation over evenly spaced samples."""
    n = len(values)
    if n < 3:
        return values[-1], 0.0
    xs = list(range(n))
    x_mean = mean(xs)
    y_mean = mean(values)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    den = sum((x - x_mean) ** 2 for x in xs) or 1.0
    slope = num / den
    predicted = values[-1] + slope * horizon_points
    return predicted, slope


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# Pooler/driver hint per engine — same "go configure this" style as
# services/dashboard_snapshot.py's connectivity recommendations, kept generic (no specific
# numeric target) so it can't contradict parameter_audit's own, more precise findings for the
# same setting (Faz 15 İŞ 6 — see SORULAR.md).
_CONNECTION_ACTIONS = {
    "postgresql": ("max_connections değerini ve bir connection pooler (PgBouncer/pgpool-II) kullanımını değerlendirin.", "SHOW max_connections;"),
    "sqlserver": ("Bağlantı havuzlama (connection pooling) ayarlarını ve uygulama tarafı pool boyutunu gözden geçirin.", None),
    "mongodb": ("Sürücü tarafı bağlantı havuzu (maxPoolSize) ayarını gözden geçirin.", None),
}


async def run_predictions(
    session: AsyncSession,
    instance_id: int,
    current_metrics: dict,
    *,
    engine: str = "postgresql",
    horizon_minutes: int = 60,
    sample_interval_seconds: int = 15,
) -> list[PredictionInsight]:
    """Detect likely threshold breaches using trend extrapolation."""
    horizon_points = max(int(horizon_minutes * 60 / sample_interval_seconds), 4)
    watch_metrics = (
        "connection_utilization_pct",
        "active_connections",
        "cache_hit_ratio",
        "replication_lag_bytes",
        "ops_per_sec",
        "transactions_per_sec",
        "database_size_bytes",
    )
    created: list[PredictionInsight] = []

    for metric_key in watch_metrics:
        if metric_key not in current_metrics or current_metrics[metric_key] is None:
            continue
        if metric_key not in METRIC_KEYS and metric_key not in {
            "connection_utilization_pct",
            "ops_per_sec",
        }:
            continue

        result = await session.execute(
            select(MetricSample)
            .where(MetricSample.instance_id == instance_id)
            .order_by(MetricSample.collected_at.desc())
            .limit(40)
        )
        samples = list(reversed(result.scalars().all()))
        series: list[float] = []
        for sample in samples:
            val = sample.get_metric(metric_key)
            if val is not None:
                series.append(float(val))
        current = float(current_metrics[metric_key])
        series.append(current)
        if len(series) < 5:
            continue

        predicted, slope = _linear_forecast(series, horizon_points)

        if metric_key == "database_size_bytes":
            outcome = _storage_growth_risk(current, slope, sample_interval_seconds)
            if outcome is None:
                continue
            severity, message, recommendation, action = outcome
            threshold = current  # not a real breach threshold — kept for the row's own record
        else:
            threshold, severity, message = _default_risk(metric_key, current, predicted, slope)
            if threshold is None:
                continue
            if not _will_breach(metric_key, current, predicted, threshold):
                continue
            recommendation, action = _recommendation_for(metric_key, engine, current, predicted)

        existing = await session.execute(
            select(PredictionInsight).where(
                PredictionInsight.instance_id == instance_id,
                PredictionInsight.metric_key == metric_key,
                PredictionInsight.acknowledged_at.is_(None),
            )
        )
        if existing.scalar_one_or_none():
            continue

        insight = PredictionInsight(
            instance_id=instance_id,
            metric_key=metric_key,
            horizon_minutes=horizon_minutes,
            current_value=current,
            predicted_value=round(predicted, 2),
            threshold=threshold,
            confidence=min(0.95, 0.5 + abs(slope) * 0.05),
            severity=severity,
            message=message,
            recommendation=recommendation,
            action=action,
        )
        session.add(insight)
        created.append(insight)

    return created


def _default_risk(
    metric_key: str, current: float, predicted: float, slope: float
) -> tuple[float | None, str, str]:
    if metric_key in ("connection_utilization_pct", "active_connections"):
        threshold = 85.0 if metric_key == "connection_utilization_pct" else 100.0
        return (
            threshold,
            "critical" if predicted >= threshold else "warning",
            f"{metric_key} yükseliş trendinde (şimdi {current:.1f}, tahmin ~{predicted:.1f})",
        )
    if metric_key == "cache_hit_ratio":
        threshold = 90.0
        if slope >= 0:
            return None, "info", ""
        return (
            threshold,
            "warning",
            f"Cache hit oranı düşüyor (şimdi {current:.1f}%, tahmin ~{predicted:.1f}%)",
        )
    if metric_key == "replication_lag_bytes":
        threshold = 10 * 1024 * 1024
        return (
            threshold,
            "warning",
            f"Replication lag artabilir (şimdi {current:.0f} byte, tahmin ~{predicted:.0f})",
        )
    if metric_key in ("transactions_per_sec", "ops_per_sec"):
        if slope <= 0:
            return None, "info", ""
        threshold = current * 2 + 1
        return (
            threshold,
            "info",
            f"Yük artışı bekleniyor ({metric_key}: ~{predicted:.1f})",
        )
    return None, "info", ""


def _will_breach(metric_key: str, current: float, predicted: float, threshold: float) -> bool:
    if metric_key == "cache_hit_ratio":
        return predicted < threshold and current >= threshold - 5
    return predicted >= threshold or (current >= threshold * 0.85 and predicted > current)


def _recommendation_for(
    metric_key: str, engine: str, current: float, predicted: float
) -> tuple[str | None, str | None]:
    """Actionable follow-up text (+ optional copy-pasteable command) shown alongside the
    trend — deliberately generic/non-numeric where a specific setting is involved, so it can
    never contradict parameter_audit's own (more precise, live-value-aware) recommendation for
    that same setting (Faz 15 İŞ 6, see SORULAR.md)."""
    if metric_key in ("connection_utilization_pct", "active_connections"):
        return _CONNECTION_ACTIONS.get(engine, _CONNECTION_ACTIONS["postgresql"])
    if metric_key == "cache_hit_ratio":
        # Deliberately not "artırın shared_buffers" here — parameter_audit already owns that
        # specific, value-aware recommendation; cross-reference instead of duplicating it.
        return "Parametreler sekmesindeki bellek/cache ayarlarını (ör. shared_buffers) gözden geçirin.", None
    if metric_key == "replication_lag_bytes":
        return "Replika sunucunun ağ/IO gecikmesini ve kaynak kullanımını kontrol edin.", None
    if metric_key in ("transactions_per_sec", "ops_per_sec"):
        return "Artan yükü karşılamak için bağlantı havuzu ve kapasite planlaması yapın.", None
    return None, None


def _storage_growth_risk(
    current_bytes: float, slope_per_sample: float, sample_interval_seconds: int
) -> tuple[str, str, str, str | None] | None:
    """Database-size growth trend → an estimated "doubling date" (Faz 15 İŞ 6's "tahmini dolma
    tarihi"). dbace collects database_size_bytes, not host-level free/total disk space (no
    host-agent metric for that exists yet — see SORULAR.md), so this is deliberately framed as
    a data-growth projection ("doubles by ~date") rather than a disk-capacity fill date it has
    no way to actually know."""
    if slope_per_sample <= 0 or current_bytes <= 0:
        return None
    bytes_per_day = slope_per_sample * (86400 / sample_interval_seconds)
    if bytes_per_day <= 0:
        return None
    days_to_double = current_bytes / bytes_per_day
    if days_to_double > 180:
        return None  # too slow a trend to be worth surfacing as a prediction

    doubling_date = (datetime.now(UTC) + timedelta(days=days_to_double)).date().isoformat()
    severity = "critical" if days_to_double < 14 else "warning" if days_to_double < 45 else "info"
    message = (
        f"Veritabanı boyutu büyüyor (şimdi {_format_bytes(current_bytes)}, günlük "
        f"~{_format_bytes(bytes_per_day)}) — bu hızla ~{doubling_date} civarında iki katına "
        f"çıkabilir (gerçek disk kapasitesi dbace'de izlenmiyor, bu sadece veri büyüme trendi)."
    )
    recommendation = (
        "En hızlı büyüyen tabloları belirleyip arşivleme/partitioning değerlendirin, eski/soğuk "
        "veriyi temizleyin, VACUUM ile boşluğu geri kazanın, gerekirse disk kapasitesini büyütün."
    )
    return severity, message, recommendation, None
