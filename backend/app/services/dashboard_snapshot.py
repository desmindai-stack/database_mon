from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.collectors.base import ConnectionTarget
from app.domain.engines import DatabaseEngine
from app.models import DatabaseGroup, GroupHealthSnapshot, Instance, MetricSample, Node
from app.services.cluster_health import collect_group_health
from app.services.credentials import decrypt_secret
from app.services.parameter_audit import collect_parameter_audit
from app.services.performance_insights import analyze_metrics
from app.services.prerequisites import run_prerequisite_checks

logger = logging.getLogger(__name__)

# Engine-appropriate "go look here" hint for an unreachable node — this is the one
# recommendation source that never depends on a live connection succeeding, so it's the one
# guaranteed to say something even when every other source (parameter_audit, index_advisor,
# performance_insights all need to actually reach/have collected from the database) comes up
# empty because the target genuinely can't be reached.
_LOG_HINTS = {
    "postgresql": "journalctl -u patroni -n 100 (veya systemctl status postgresql patroni)",
    "sqlserver": "Get-EventLog -LogName Application -Source MSSQLSERVER -Newest 50 (veya sc query MSSQLSERVER)",
    "mongodb": "journalctl -u mongod -n 100 (veya systemctl status mongod)",
}


def _connectivity_recommendations(group: DatabaseGroup, report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not report:
        return []
    down_nodes = report.get("down_nodes") or []
    if not down_nodes:
        return []
    names = ", ".join(f"{d['node_name']} ({d['site']})" for d in down_nodes)
    hint = _LOG_HINTS.get(group.engine, "sistem/servis loglarını kontrol edin")
    return [
        {
            "severity": "high",
            "source": "connectivity",
            "group": group.name,
            "message": f"{len(down_nodes)} düğüme erişilemiyor ({names}): servis durumunu ve ağ erişimini kontrol edin.",
            "title": f"{group.engine} servisinin ayakta olduğunu doğrulayın",
            "steps": [
                "Sunucunun ayakta ve ağdan erişilebilir olduğunu doğrulayın (ping / ssh).",
                f"{group.engine} servisinin durumunu kontrol edin (systemctl status / sc query).",
                "Servis loglarını inceleyin (aşağıdaki komut).",
            ],
            "action": hint,
        }
    ]


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
                "title": f"{finding['name']} parametresini {finding['recommendation']} olacak şekilde ayarlayın",
                "steps": [
                    finding["recommendation"],
                    "Mevcut değeri canlı olarak doğrulayın (aşağıdaki komut).",
                ],
                # A safe, real next step (check the live value) — the exact target value depends
                # on server sizing dbace doesn't collect, so we don't fabricate an ALTER SYSTEM.
                "action": f"SHOW {finding['name']};",
                # Doğrudan grubun Parametreler sekmesine (Faz 16 İŞ 5).
                "link_hint": f"/groups/{group.id}?tab=parameters",
            }
        )
    return out


async def _prerequisite_recommendations(group: DatabaseGroup, nodes: list[Node]) -> list[dict[str, Any]]:
    """Faz 16 İŞ 1: eksik/yetkisiz bir ön koşul (pg_stat_statements, VIEW SERVER STATE, ...) her
    dashboard-refresh tick'inde (parameter_audit ile aynı ucuz, katalog-taraması cadence'i)
    grubun hedef düğümü üzerinden tespit edilip burada bir öneriye dönüştürülür — kullanıcı
    "neden hiç öneri gelmiyor?" sorusunun cevabını instance detayına gitmeden dashboard'da görür."""
    if group.engine not in ("postgresql", "sqlserver") or not nodes:
        return []
    target_node = next((n for n in nodes if n.role_hint == "primary"), nodes[0])
    if target_node.instance is None:
        return []
    instance = target_node.instance
    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    try:
        checks = await run_prerequisite_checks(DatabaseEngine(group.engine), target)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for check in checks:
        if check.status not in ("missing", "unauthorized"):
            continue
        out.append(
            {
                "severity": check.severity,
                "source": "prerequisites",
                "group": group.name,
                "message": f"Ön koşul eksik — {check.name}: {check.impact}",
                "title": f"{check.name} sorununu giderin" if check.fix else f"{check.name} kontrolünü tamamlayın",
                "steps": [check.impact, "Aşağıdaki komutla düzeltin, sonra bu sayfayı yenileyin."],
                "action": check.fix,
                # Doğrudan hedef instance'ın Ön koşullar paneline (Faz 16 İŞ 5).
                "link_hint": f"/instances/{instance.id}?tab=tuning",
            }
        )
    return out


async def _load_instance_snapshots(session: AsyncSession, group_id: int) -> list[dict[str, Any]]:
    """Sequential, session-bound read — must run before any concurrent network work. Only
    pulls what analyze_metrics() (in-memory, no target-DB I/O) needs; connection details and
    slow-query text used to be fetched here too for the (now-removed, see
    _instance_recommendations) automatic index_advisor call."""
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
        snapshots.append(
            {
                "instance_id": instance.id,
                "name": instance.name,
                "engine": instance.engine,
                "metrics_json": dict(latest.metrics_json or {}) if latest else None,
                "collected_at": latest.collected_at if latest else None,
            }
        )
    return snapshots


async def _instance_recommendations(group: DatabaseGroup, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """In-memory only — analyze_metrics() works off metrics_json already sitting in the DB
    (no network I/O to the target instance at all). index_advisor recommendations used to be
    generated here too, but that meant every dashboard refresh tick (as often as every 10s,
    see ALLOWED_REFRESH_INTERVALS) ran a live catalog scan (pg_stats/pg_indexes/pg_class,
    sometimes a hypopg EXPLAIN re-plan) against every instance with a slow query — real load
    on the monitored database for a "just glance at the dashboard" action. Index advice is now
    exclusively on-demand: POST /api/queries/{id}/advice, triggered by the user opening a slow
    query's advice panel, response cached (see routers/queries.py) — see SORULAR.md."""
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
                        # insight.recommendation is prose (no single copy-pasteable command exists
                        # here) — insight.action is a UI tab hint ("queries"/"metrics"), not a
                        # command, so it's deliberately not reused as the action field.
                        "message": f"{snap['name']}: {insight.title} — {insight.recommendation}",
                        "title": insight.recommendation,
                        "steps": [f"{snap['name']} instance'ında: {insight.recommendation}"],
                        # Doğrudan ilgili instance'ın en alakalı sekmesine — grup sayfasından
                        # tekrar instance aramak yerine (Faz 16 İŞ 5, "tek tıkla ilerlesin").
                        # insight.action zaten TuningPanel'in "İlgili sekmeye git" butonunun
                        # kullandığı geçerli bir Tab adı (queries/metrics/alerts); yoksa Tuning'e
                        # düşer — dashboard'daki "sorun" ile instance detayındaki en yakın sekme
                        # arasında AYNI eşleme kullanılıyor.
                        "link_hint": (
                            f"/instances/{snap['instance_id']}?tab={insight.action}"
                            if insight.action in ("queries", "metrics", "alerts")
                            else f"/instances/{snap['instance_id']}?tab=tuning"
                        ),
                    }
                )

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
        report, param_recs, instance_recs, prereq_recs = await asyncio.gather(
            _probe_group_health(group, nodes),
            _parameter_recommendations(group, nodes),
            _instance_recommendations(group, snapshots_by_group[group.id]),
            _prerequisite_recommendations(group, nodes),
        )
        connectivity_recs = _connectivity_recommendations(group, report)
        return group.id, report, [*param_recs, *instance_recs, *prereq_recs, *connectivity_recs]

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
