from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget
from app.models import Application, Customer, DatabaseGroup, Instance, MetricSample, Node, SlowQuerySample
from app.services.cluster_health import collect_group_health
from app.services.credentials import decrypt_secret
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.parameter_audit import collect_parameter_audit
from app.services.performance_insights import analyze_metrics

logger = logging.getLogger(__name__)

_ENV_RANK = {"prod": 0, "preprod": 1, "test": 2, "dev": 3}
_SEVERITY_RANK = {"critical": 0, "high": 1, "warning": 2, "medium": 3, "low": 4, "info": 5}


def _issue_sort_key(issue: dict[str, Any]) -> tuple[int, int]:
    return (_ENV_RANK.get(issue["environment"], 9), _SEVERITY_RANK.get(issue["severity"], 9))


def _rec_sort_key(rec: dict[str, Any]) -> tuple[int, int]:
    return (_ENV_RANK.get(rec.get("_environment", "dev"), 9), _SEVERITY_RANK.get(rec["severity"], 9))


async def _group_context(session: AsyncSession) -> dict[int, dict[str, Any]]:
    rows = (
        await session.execute(
            select(DatabaseGroup, Application, Customer)
            .join(Application, DatabaseGroup.application_id == Application.id)
            .join(Customer, Application.customer_id == Customer.id)
        )
    ).all()
    return {g.id: {"group": g, "application": a, "customer": c} for g, a, c in rows}


def _issue(ctx: dict[str, Any], severity: str, message: str) -> dict[str, Any]:
    group = ctx["group"]
    return {
        "severity": severity,
        "customer": ctx["customer"].name,
        "application": ctx["application"].name,
        "group": group.name,
        "environment": group.environment,
        "message": message,
        "link_hint": f"/groups/{group.id}",
    }


def _issues_from_group_health(ctx: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    if report.get("split_brain"):
        nodes_str = ", ".join(report.get("split_brain_nodes") or [])
        issues.append(_issue(ctx, "critical", f"Split-brain şüphesi: VIP'i tutan düğümler — {nodes_str}"))

    etcd = report.get("etcd_quorum") or {}
    if etcd.get("total") and not etcd.get("has_quorum", True):
        issues.append(_issue(ctx, "critical", f"etcd quorum kaybedildi ({etcd.get('up')}/{etcd.get('total')})"))

    for down in report.get("down_nodes") or []:
        issues.append(_issue(ctx, "critical", f"Düğüm erişilemez: {down['node_name']} ({down['site']})"))

    cluster = report.get("cluster")
    if cluster is not None and not cluster.get("has_leader", True):
        issues.append(_issue(ctx, "critical", "Cluster lider yok (no leader)"))

    if report.get("overall") == "warning" and not issues:
        issues.append(_issue(ctx, "warning", "Servis durumu warning (bazı servisler doğrulanamadı)"))

    return issues


async def _probe_group_health(group: DatabaseGroup, nodes: list[Node]) -> dict[str, Any] | None:
    if not nodes:
        return None
    try:
        return await collect_group_health(group, nodes)
    except Exception:
        logger.warning("dashboard: group health probe failed for group %s", group.id)
        return None


async def _parameter_recommendations(ctx: dict[str, Any], nodes: list[Node]) -> list[dict[str, Any]]:
    group = ctx["group"]
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
                "_environment": group.environment,
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


async def _instance_recommendations(ctx: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Network-bound only (no app DB session) — safe to run concurrently across groups."""
    group = ctx["group"]
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
                        "_environment": group.environment,
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
                            "_environment": group.environment,
                        }
                    )
            except Exception:
                logger.debug("dashboard: index advisor failed for instance %s", snap["name"])

    return out


async def collect_dashboard_summary(session: AsyncSession) -> dict[str, Any]:
    totals = {
        "customers": (await session.execute(select(func.count()).select_from(Customer))).scalar_one(),
        "applications": (await session.execute(select(func.count()).select_from(Application))).scalar_one(),
        "groups": (await session.execute(select(func.count()).select_from(DatabaseGroup))).scalar_one(),
        "nodes": (await session.execute(select(func.count()).select_from(Node))).scalar_one(),
    }

    context_by_group = await _group_context(session)

    all_nodes = (await session.execute(select(Node))).scalars().all()
    nodes_by_group: dict[int, list[Node]] = {}
    for node in all_nodes:
        nodes_by_group.setdefault(node.group_id, []).append(node)

    # Session-bound reads happen sequentially, before any concurrent network I/O below
    # (AsyncSession is not safe to use from multiple coroutines at once).
    snapshots_by_group = {
        group_id: await _load_instance_snapshots(session, group_id) for group_id in context_by_group
    }

    async def handle_group(group_id: int) -> tuple[int, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        ctx = context_by_group[group_id]
        nodes = nodes_by_group.get(group_id, [])
        report, param_recs, instance_recs = await asyncio.gather(
            _probe_group_health(ctx["group"], nodes),
            _parameter_recommendations(ctx, nodes),
            _instance_recommendations(ctx, snapshots_by_group[group_id]),
        )
        return group_id, report, param_recs, instance_recs

    results = await asyncio.gather(*(handle_group(gid) for gid in context_by_group))

    health_counts = {"critical": 0, "warning": 0, "healthy": 0, "unknown": 0}
    all_issues: list[dict[str, Any]] = []
    all_recommendations: list[dict[str, Any]] = []

    for group_id, report, param_recs, instance_recs in results:
        ctx = context_by_group[group_id]
        nodes = nodes_by_group.get(group_id, [])
        if not nodes or report is None:
            health_counts["unknown"] += 1
        else:
            overall = report.get("overall", "unknown")
            health_counts[overall] = health_counts.get(overall, 0) + 1
            all_issues.extend(_issues_from_group_health(ctx, report))
        all_recommendations.extend(param_recs)
        all_recommendations.extend(instance_recs)

    all_issues.sort(key=_issue_sort_key)
    all_recommendations.sort(key=_rec_sort_key)
    for rec in all_recommendations:
        rec.pop("_environment", None)

    return {
        "totals": totals,
        "health": health_counts,
        "top_issues": all_issues[:10],
        "recommendations": all_recommendations[:10],
    }
