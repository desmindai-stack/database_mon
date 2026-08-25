from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Application, Customer, DatabaseGroup, GroupHealthSnapshot, Node

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
    """One issue per concrete problem — but a cluster's down nodes are reported as a single
    grup-level issue (not one row per node) so a multi-node outage doesn't flood top_issues."""
    issues: list[dict[str, Any]] = []

    if report.get("split_brain"):
        nodes_str = ", ".join(report.get("split_brain_nodes") or [])
        issues.append(_issue(ctx, "critical", f"Split-brain şüphesi: VIP'i tutan düğümler — {nodes_str}"))

    etcd = report.get("etcd_quorum") or {}
    if etcd.get("total") and not etcd.get("has_quorum", True):
        issues.append(_issue(ctx, "critical", f"etcd quorum kaybedildi ({etcd.get('up')}/{etcd.get('total')})"))

    down_nodes = report.get("down_nodes") or []
    if down_nodes:
        names = ", ".join(f"{d['node_name']} ({d['site']})" for d in down_nodes)
        issues.append(_issue(ctx, "critical", f"{len(down_nodes)} düğüm erişilemez: {names}"))

    cluster = report.get("cluster")
    if cluster is not None and not cluster.get("has_leader", True):
        issues.append(_issue(ctx, "critical", "Cluster lider yok (no leader)"))

    if report.get("overall") == "warning" and not issues:
        issues.append(_issue(ctx, "warning", "Servis durumu warning (bazı servisler doğrulanamadı)"))

    return issues


async def collect_dashboard_summary(session: AsyncSession) -> dict[str, Any]:
    """Reads the last collector-refreshed snapshot for every group — no live network probing.

    See services.dashboard_snapshot.refresh_all_group_snapshots for the collector that
    populates GroupHealthSnapshot (runs on the scheduler + POST /api/dashboard/refresh);
    this function only ever does DB reads, keeping GET /api/dashboard/summary well under a second.
    """
    totals = {
        "customers": (await session.execute(select(func.count()).select_from(Customer))).scalar_one(),
        "applications": (await session.execute(select(func.count()).select_from(Application))).scalar_one(),
        "groups": (await session.execute(select(func.count()).select_from(DatabaseGroup))).scalar_one(),
        "nodes": (await session.execute(select(func.count()).select_from(Node))).scalar_one(),
    }

    context_by_group = await _group_context(session)
    snapshots = (await session.execute(select(GroupHealthSnapshot))).scalars().all()
    snapshot_by_group = {s.group_id: s for s in snapshots}

    health_counts = {"critical": 0, "warning": 0, "healthy": 0, "unknown": 0}
    all_issues: list[dict[str, Any]] = []
    all_recommendations: list[dict[str, Any]] = []
    checked_ats: list[datetime] = []

    for group_id, ctx in context_by_group.items():
        snapshot = snapshot_by_group.get(group_id)
        if snapshot is None:
            health_counts["unknown"] += 1
            continue

        checked_ats.append(snapshot.checked_at)
        health_counts[snapshot.overall] = health_counts.get(snapshot.overall, 0) + 1
        if snapshot.report_json:
            all_issues.extend(_issues_from_group_health(ctx, snapshot.report_json))

        for rec in snapshot.recommendations_json or []:
            all_recommendations.append({**rec, "_environment": ctx["group"].environment})

    all_issues.sort(key=_issue_sort_key)
    all_recommendations.sort(key=_rec_sort_key)
    for rec in all_recommendations:
        rec.pop("_environment", None)

    return {
        "totals": totals,
        "health": health_counts,
        "top_issues": all_issues[:10],
        "recommendations": all_recommendations[:10],
        "last_checked": min(checked_ats) if checked_ats else None,
    }
