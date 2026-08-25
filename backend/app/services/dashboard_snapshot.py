from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.collectors.base import ConnectionTarget
from app.models import DatabaseGroup, GroupHealthSnapshot, Instance, MetricSample, Node, SlowQuerySample
from app.services.cluster_health import collect_group_health
from app.services.credentials import decrypt_secret
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.parameter_audit import collect_parameter_audit
from app.services.performance_insights import analyze_metrics

logger = logging.getLogger(__name__)


async def _probe_group_health(group: DatabaseGroup, nodes: list[Node]) -> dict[str, Any] | None:
    if not nodes:
        return None
    try:
        return await collect_group_health(group, nodes)
    except Exception:
        logger.warning("dashboard snapshot: group health probe failed for group %s", group.id)
        return None


async def _parameter_recommendations(group: DatabaseGroup, nodes: list[Node]) -> list[dict[str, Any]]:
    if group.engine != "postgresql" or not nodes:
        return []
    try:
        report = await collect_parameter_audit(group, nodes)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for finding in report["findings"]:
        if finding["severity"] in ("ok", "unknown"):
            continue
        out.append(
            {
                "severity": finding["severity"],
                "source": "parameter_audit",
                "group": group.name,
                "message": f"{finding['name']}: {finding['recommendation']}",
            }
        )
    return out


async def _load_instance_snapshots(session: AsyncSession, group_id: int) -> list[dict[str, Any]]:
    """Sequential, session-bound read — must run before any concurrent network work."""
    instances = (await session.execute(select(Instance).where(Instance.group_id == group_id))).scalars().all()
    snapshots: list[dict[str, Any]] = []
    for instance in instances:
        latest = (
            await session.execute(
                select(MetricSample)
                .where(MetricSample.instance_id == instance.id)
                .order_by(MetricSample.collected_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        slow = (
            await session.execute(
                select(SlowQuerySample)
                .where(SlowQuerySample.instance_id == instance.id)
                .order_by(SlowQuerySample.mean_time_ms.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        snapshots.append(
            {
                "name": instance.name,
                "engine": instance.engine,
                "host": instance.host,
                "port": instance.port,
                "database": instance.database,
                "username": instance.username,
                "password_encrypted": instance.password,
                "options": instance.options,
                "metrics_json": dict(latest.metrics_json or {}) if latest else None,
                "collected_at": latest.collected_at if latest else None,
                "slow_query": slow.query if slow else None,
                "slow_query_mean_ms": slow.mean_time_ms if slow else None,
            }
        )
    return snapshots


async def _instance_recommendations(group: DatabaseGroup, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Network-bound only (no app DB session) — safe to run concurrently across groups."""
    out: list[dict[str, Any]] = []

    for snap in snapshots:
        if snap["metrics_json"] is not None:
            tuning = analyze_metrics(snap["metrics_json"], slow_queries=[], collected_at=snap["collected_at"])
            for insight in tuning.insights:
                if insight.severity not in ("critical", "high", "medium"):
                    continue
                out.append(
                    {
                        "severity": insight.severity,
                        "source": "performance_insights",
                        "group": group.name,
                        "message": f"{snap['name']}: {insight.title}",
                    }
                )

        if snap["engine"] == "postgresql" and snap["slow_query"] and (snap["slow_query_mean_ms"] or 0) > 100:
            try:
                target = ConnectionTarget(
                    host=snap["host"],
                    port=snap["port"],
                    database=snap["database"],
                    username=snap["username"],
                    password=decrypt_secret(snap["password_encrypted"]),
                    options=snap["options"],
                )
                advisor = PostgreSQLIndexAdvisor(target)
                advice_list = await advisor.advise(snap["slow_query"])
                for advice in advice_list[:1]:
                    out.append(
                        {
                            "severity": "medium",
                            "source": "index_advisor",
                            "group": group.name,
                            "message": f"{snap['name']}: {advice.reason} -> {advice.index_ddl}",
                        }
                    )
            except Exception:
                logger.debug("dashboard snapshot: index advisor failed for instance %s", snap["name"])

    return out


async def refresh_all_group_snapshots(session: AsyncSession) -> int:
    """Live-probe every group's health + recommendations and persist as GroupHealthSnapshot rows.

    Runs on the collector scheduler (worker/all run_mode) and via POST /api/dashboard/refresh —
    GET /api/dashboard/summary only ever reads what this wrote, so it stays fast regardless of
    how many groups/nodes exist or how slow they are to reach.
    """
    groups = list((await session.execute(select(DatabaseGroup))).scalars().all())
    if not groups:
        return 0

    all_nodes = (
        await session.execute(select(Node).options(selectinload(Node.instance), selectinload(Node.server)))
    ).scalars().all()
    nodes_by_group: dict[int, list[Node]] = {}
    for node in all_nodes:
        nodes_by_group.setdefault(node.group_id, []).append(node)

    # Session-bound reads happen sequentially, before any concurrent network I/O below
    # (AsyncSession is not safe to use from multiple coroutines at once).
    snapshots_by_group = {group.id: await _load_instance_snapshots(session, group.id) for group in groups}

    async def handle_group(group: DatabaseGroup) -> tuple[int, dict[str, Any] | None, list[dict[str, Any]]]:
        nodes = nodes_by_group.get(group.id, [])
        report, param_recs, instance_recs = await asyncio.gather(
            _probe_group_health(group, nodes),
            _parameter_recommendations(group, nodes),
            _instance_recommendations(group, snapshots_by_group[group.id]),
        )
        return group.id, report, [*param_recs, *instance_recs]

    results = await asyncio.gather(*(handle_group(group) for group in groups))

    now = datetime.now(UTC)
    for group_id, report, recommendations in results:
        overall = (report or {}).get("overall", "unknown")
        existing = (
            await session.execute(select(GroupHealthSnapshot).where(GroupHealthSnapshot.group_id == group_id))
        ).scalar_one_or_none()
        if existing is None:
            existing = GroupHealthSnapshot(group_id=group_id)
            session.add(existing)
        existing.overall = overall
        existing.report_json = report
        existing.recommendations_json = recommendations
        existing.checked_at = now

    await session.commit()
    return len(groups)


async def group_status_summaries(session: AsyncSession, group_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Compact per-group status (used by the group list + Group Detail header) built from the
    same GroupHealthSnapshot cache the dashboard reads — no extra live probing."""
    if not group_ids:
        return {}

    snapshots = (
        await session.execute(select(GroupHealthSnapshot).where(GroupHealthSnapshot.group_id.in_(group_ids)))
    ).scalars().all()
    snapshot_by_group = {s.group_id: s for s in snapshots}

    nodes = (await session.execute(select(Node).where(Node.group_id.in_(group_ids)))).scalars().all()
    primary_by_group: dict[int, str] = {}
    for node in nodes:
        if node.role_hint == "primary":
            primary_by_group[node.group_id] = node.name

    summaries: dict[int, dict[str, Any]] = {}
    for group_id in group_ids:
        snapshot = snapshot_by_group.get(group_id)
        if snapshot is None:
            summaries[group_id] = {"primary_node": primary_by_group.get(group_id)}
            continue

        report = snapshot.report_json or {}
        cluster = report.get("cluster") or {}
        max_lag: float | None = None
        for member in cluster.get("members") or []:
            lag = member.get("lag")
            if isinstance(lag, (int, float)):
                max_lag = float(lag) if max_lag is None else max(max_lag, float(lag))

        down_nodes = report.get("down_nodes") or []
        total_nodes = len(report.get("nodes") or [])
        summaries[group_id] = {
            "overall": snapshot.overall,
            "nodes_up": max(total_nodes - len(down_nodes), 0),
            "nodes_down": len(down_nodes),
            "primary_node": cluster.get("leader") or primary_by_group.get(group_id),
            "replication_lag_bytes": max_lag,
            "checked_at": snapshot.checked_at,
        }
    return summaries
