from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertEvent, AlertRule, MetricSample

METRIC_FIELDS = {
    "active_connections": "active_connections",
    "cache_hit_ratio": "cache_hit_ratio",
    "transactions_per_sec": "transactions_per_sec",
    "replication_lag_bytes": "replication_lag_bytes",
    "database_size_bytes": "database_size_bytes",
    "deadlocks": "deadlocks",
    "temp_bytes": "temp_bytes",
}


def _compare(value: float, operator: str, threshold: float) -> bool:
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == "==":
        return value == threshold
    return False


async def evaluate_alerts(session: AsyncSession, instance_id: int, metrics: dict) -> None:
    result = await session.execute(
        select(AlertRule).where(
            AlertRule.enabled.is_(True),
            (AlertRule.instance_id == instance_id) | (AlertRule.instance_id.is_(None)),
        )
    )
    rules = result.scalars().all()

    for rule in rules:
        field = METRIC_FIELDS.get(rule.metric)
        if not field:
            continue
        raw = metrics.get(field)
        if raw is None:
            continue
        value = float(raw)
        if not _compare(value, rule.operator, rule.threshold):
            continue

        existing = await session.execute(
            select(AlertEvent).where(
                AlertEvent.rule_id == rule.id,
                AlertEvent.instance_id == instance_id,
                AlertEvent.resolved_at.is_(None),
            )
        )
        if existing.scalar_one_or_none():
            continue

        session.add(
            AlertEvent(
                rule_id=rule.id,
                instance_id=instance_id,
                metric_value=value,
                message=(
                    f"{rule.name}: {rule.metric} {rule.operator} {rule.threshold} "
                    f"(current: {value})"
                ),
            )
        )


async def get_latest_metrics(session: AsyncSession, instance_id: int) -> MetricSample | None:
    result = await session.execute(
        select(MetricSample)
        .where(MetricSample.instance_id == instance_id)
        .order_by(MetricSample.collected_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
