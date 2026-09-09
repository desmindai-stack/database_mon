from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertEvent, AlertRule
from app.services.finding_dependencies import suppressed_alert_metrics

CLUSTER_RULE_SPECS = [
    ("Patroni down", "patroni_down", ">", 0),
    ("etcd unhealthy", "etcd_down", ">", 0),
    ("HAProxy down", "haproxy_down", ">", 0),
    ("Keepalived VIP unreachable", "keepalived_vip_down", ">", 0),
    ("Cluster no leader", "cluster_has_leader", "<", 1),
    ("Cluster services down", "cluster_services_down", ">", 0),
]

# Group-level (multi-node) health flags — produced by
# services.cluster_health.group_health_metric_flags() and persisted via
# ensure_group_alert_rules()/evaluate_group_alerts() below, keyed by
# AlertRule.group_id / AlertEvent.group_id instead of instance_id.
GROUP_RULE_SPECS = [
    ("Replication lag high", "replication_lag_bytes", ">", 50_000_000),
    ("etcd quorum lost", "etcd_quorum_lost", ">", 0),
    ("Split-brain detected", "split_brain", ">", 0),
    ("Node down", "node_down", ">", 0),
]


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


async def ensure_cluster_alert_rules(session: AsyncSession, instance_id: int) -> None:
    """Create default cluster health alert rules for an instance if missing."""
    existing = (
        await session.execute(select(AlertRule).where(AlertRule.instance_id == instance_id))
    ).scalars().all()
    existing_metrics = {r.metric for r in existing}
    for name, metric, operator, threshold in CLUSTER_RULE_SPECS:
        if metric in existing_metrics:
            continue
        session.add(
            AlertRule(
                instance_id=instance_id,
                name=name,
                metric=metric,
                operator=operator,
                threshold=float(threshold),
                enabled=True,
                is_default=True,
            )
        )
    await session.flush()


async def evaluate_alerts(session: AsyncSession, instance_id: int, metrics: dict) -> None:
    result = await session.execute(
        select(AlertRule).where(
            AlertRule.enabled.is_(True),
            AlertRule.rule_type == "metric",
            (AlertRule.instance_id == instance_id) | (AlertRule.instance_id.is_(None)),
        )
    )
    rules = result.scalars().all()

    # BAĞIMLILIK BASTIRMASI (Faz 28 İŞ 2). Bir düğüm düştüğünde ya da Patroni kapandığında
    # ona bağlı eşik alarmları da tetikleniyor ve 40 e-posta gidiyordu. Kök sebep alarmının
    # KENDİSİ bastırılmıyor — bastırılsaydı hiç haber gitmezdi ki bu, 40 e-postadan çok daha
    # kötü olurdu.
    #
    # Grafik rapor tarafıyla AYNI yerden geliyor (services/finding_dependencies.py): alarm
    # tarafına ayrı bir liste yazmak, iki listenin zamanla ayrışması demekti.
    suppressed = suppressed_alert_metrics(metrics)

    for rule in rules:
        if rule.metric in suppressed:
            continue
        raw = metrics.get(rule.metric)
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


async def ensure_group_alert_rules(session: AsyncSession, group_id: int) -> None:
    """Create default group-health alert rules for a DatabaseGroup if missing."""
    existing = (
        await session.execute(select(AlertRule).where(AlertRule.group_id == group_id))
    ).scalars().all()
    existing_metrics = {r.metric for r in existing}
    for name, metric, operator, threshold in GROUP_RULE_SPECS:
        if metric in existing_metrics:
            continue
        session.add(
            AlertRule(
                group_id=group_id,
                name=name,
                metric=metric,
                operator=operator,
                threshold=float(threshold),
                enabled=True,
                is_default=True,
            )
        )
    await session.flush()


async def evaluate_group_alerts(session: AsyncSession, group_id: int, metrics: dict) -> None:
    result = await session.execute(
        select(AlertRule).where(
            AlertRule.enabled.is_(True), AlertRule.rule_type == "metric", AlertRule.group_id == group_id
        )
    )
    rules = result.scalars().all()
    # Grup seviyesinde de aynı grafik: quorum kaybında "lider yok" alarmı üretilmiyor, çünkü
    # quorum olmadan lider seçilemez — lidersizlik bu durumun sonucu, ayrı bir arıza değil.
    suppressed = suppressed_alert_metrics(metrics)

    for rule in rules:
        if rule.metric in suppressed:
            continue
        raw = metrics.get(rule.metric)
        if raw is None:
            continue
        value = float(raw)
        if not _compare(value, rule.operator, rule.threshold):
            continue

        existing = await session.execute(
            select(AlertEvent).where(
                AlertEvent.rule_id == rule.id,
                AlertEvent.group_id == group_id,
                AlertEvent.resolved_at.is_(None),
            )
        )
        if existing.scalar_one_or_none():
            continue

        session.add(
            AlertEvent(
                rule_id=rule.id,
                group_id=group_id,
                metric_value=value,
                message=(
                    f"{rule.name}: {rule.metric} {rule.operator} {rule.threshold} "
                    f"(current: {value})"
                ),
            )
        )
