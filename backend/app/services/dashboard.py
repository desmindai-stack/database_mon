from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Application, Customer, DatabaseGroup, GroupHealthSnapshot, Node

_ENV_RANK = {"prod": 0, "preprod": 1, "test": 2, "dev": 3}
_SEVERITY_RANK = {"critical": 0, "high": 1, "warning": 2, "medium": 3, "low": 4, "info": 5}
_STATUS_RANK = {"critical": 0, "warning": 1, "unknown": 2, "healthy": 3}


def _group_row_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    return (_STATUS_RANK.get(row["status"], 9), _ENV_RANK.get(row["environment"], 9))


def _issue_sort_key(issue: dict[str, Any]) -> tuple[int, int]:
    # Severity first (critical before warning) so the worst problems are always on top,
    # regardless of environment; prod only breaks ties within the same severity.
    return (_SEVERITY_RANK.get(issue["severity"], 9), _ENV_RANK.get(issue["environment"], 9))


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


def _issue(ctx: dict[str, Any], severity: str, message: str, checked_at: datetime | None, node: str | None = None) -> dict[str, Any]:
    group = ctx["group"]
    return {
        "severity": severity,
        "customer": ctx["customer"].name,
        "application": ctx["application"].name,
        "group": group.name,
        "node": node,
        "environment": group.environment,
        "message": message,
        "link_hint": f"/groups/{group.id}",
        "checked_at": checked_at,
        "_group_id": group.id,
    }


def _issues_from_group_health(ctx: dict[str, Any], report: dict[str, Any], checked_at: datetime) -> list[dict[str, Any]]:
    """One issue per concrete problem — but a cluster's down nodes are reported as a single
    grup-level issue (not one row per node) so a multi-node outage doesn't flood top_issues."""
    issues: list[dict[str, Any]] = []

    if report.get("split_brain"):
        nodes_str = ", ".join(report.get("split_brain_nodes") or [])
        issues.append(_issue(ctx, "critical", f"Split-brain şüphesi: VIP'i tutan düğümler — {nodes_str}", checked_at, node=nodes_str))

    etcd = report.get("etcd_quorum") or {}
    if etcd.get("total") and not etcd.get("has_quorum", True):
        issues.append(_issue(ctx, "critical", f"etcd quorum kaybedildi ({etcd.get('up')}/{etcd.get('total')})", checked_at))

    down_nodes = report.get("down_nodes") or []
    if down_nodes:
        names = ", ".join(f"{d['node_name']} ({d['site']})" for d in down_nodes)
        issues.append(_issue(ctx, "critical", f"{len(down_nodes)} düğüm erişilemez: {names}", checked_at, node=names))

    cluster = report.get("cluster")
    if cluster is not None and not cluster.get("has_leader", True):
        issues.append(_issue(ctx, "critical", "Cluster lider yok (no leader)", checked_at))

    if report.get("overall") == "warning" and not issues:
        issues.append(_issue(ctx, "warning", "Servis durumu warning (bazı servisler doğrulanamadı)", checked_at))

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
    recs_by_group: dict[int, list[dict[str, Any]]] = {}
    checked_ats: list[datetime] = []
    # One row per group regardless of status — top_issues only ever contains groups with an
    # active problem, so it can't be the target of the dashboard's stat-card filter (Faz 15 İŞ
    # 3): clicking "Sağlıklı"/"Bilinmiyor" needs somewhere to show something.
    group_rows: list[dict[str, Any]] = []

    for group_id, ctx in context_by_group.items():
        snapshot = snapshot_by_group.get(group_id)
        group = ctx["group"]
        if snapshot is None:
            health_counts["unknown"] += 1
            group_rows.append(
                {
                    "group_id": group_id,
                    "group": group.name,
                    "customer": ctx["customer"].name,
                    "application": ctx["application"].name,
                    "environment": group.environment,
                    "status": "unknown",
                    "link_hint": f"/groups/{group_id}",
                }
            )
            continue

        # SQLite doesn't actually persist tzinfo — a row re-queried fresh comes back naive, but
        # one still cached in this session's identity map (e.g. right after this same session
        # wrote it) keeps whatever tzinfo Python set. min() can't compare the two, so normalize
        # to naive here; every value is UTC-instant either way, only the label differs.
        checked_at = snapshot.checked_at
        checked_ats.append(checked_at.replace(tzinfo=None) if checked_at.tzinfo else checked_at)
        health_counts[snapshot.overall] = health_counts.get(snapshot.overall, 0) + 1
        group_rows.append(
            {
                "group_id": group_id,
                "group": group.name,
                "customer": ctx["customer"].name,
                "application": ctx["application"].name,
                "environment": group.environment,
                "status": snapshot.overall,
                "link_hint": f"/groups/{group_id}",
            }
        )
        if snapshot.report_json:
            all_issues.extend(_issues_from_group_health(ctx, snapshot.report_json, snapshot.checked_at))

        group_recs = []
        for rec in snapshot.recommendations_json or []:
            enriched = {
                **rec,
                "_environment": ctx["group"].environment,
                "customer": ctx["customer"].name,
                "application": ctx["application"].name,
                "environment": ctx["group"].environment,
                "link_hint": f"/groups/{group_id}",
                "checked_at": snapshot.checked_at,
                "steps": rec.get("steps") or [],
            }
            all_recommendations.append(enriched)
            group_recs.append(enriched)
        group_recs.sort(key=_rec_sort_key)
        recs_by_group[group_id] = group_recs

    all_issues.sort(key=_issue_sort_key)
    all_recommendations.sort(key=_rec_sort_key)
    group_rows.sort(key=_group_row_sort_key)

    # Attach each issue's own group's best (highest-severity) recommendation, if it has one —
    # dbace has no causal link between a specific issue and a specific recommendation (they
    # come from independent diagnostic subsystems: cluster health vs. parameter audit vs.
    # performance insights/index advisor), so "the group's top recommendation" is the closest
    # available approximation of "the fix for this row" (see SORULAR.md).
    for issue in all_issues:
        group_id = issue.pop("_group_id", None)
        best = recs_by_group.get(group_id) or []
        if best:
            rec = dict(best[0])
            rec.pop("_environment", None)
            issue["recommendation"] = rec
        else:
            issue["recommendation"] = None

    for rec in all_recommendations:
        rec.pop("_environment", None)

    return {
        "totals": totals,
        "health": health_counts,
        "top_issues": all_issues[:10],
        "recommendations": all_recommendations[:10],
        "groups": group_rows,
        "last_checked": min(checked_ats) if checked_ats else None,
    }
