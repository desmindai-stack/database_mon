from __future__ import annotations

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


async def run_predictions(
    session: AsyncSession,
    instance_id: int,
    current_metrics: dict,
    *,
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
        threshold, severity, message = _default_risk(metric_key, current, predicted, slope)
        if threshold is None:
            continue

        will_breach = _will_breach(metric_key, current, predicted, threshold)
        if not will_breach:
            continue

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
