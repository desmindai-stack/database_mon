from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from app.models import DatabaseGroup, Instance, Node

logger = logging.getLogger(__name__)

DEFAULT_OPTIONS = {
    "patroni_port": 8008,
    "etcd_port": 2379,
    "haproxy_stats_port": 8404,
    "haproxy_stats_path": "/stats;csv",
    "keepalived_vip": None,
    "probe_timeout_sec": 3,
    "patroni_tls": False,
    "agent_url": None,
    "agent_token": None,
}

# Engine -> the single service name used for a plain "is the DB port reachable" check
# (standalone topology, or as the base connectivity signal generally). Getting this right
# matters: a SQL Server node reported as "postgresql: down" is misleading, not just cosmetic.
_ENGINE_SERVICE_NAME = {"postgresql": "postgresql", "sqlserver": "sqlserver", "mongodb": "mongodb"}


def _opts(instance: Instance) -> dict[str, Any]:
    raw = dict(DEFAULT_OPTIONS)
    raw.update(instance.options or {})
    return raw


def _services(instance: Instance) -> set[str]:
    services = instance.services or []
    if not services:
        # If none selected, probe the common Patroni stack by default when cluster_name set.
        if instance.cluster_name:
            return {"etcd", "patroni", "postgresql", "keepalived", "haproxy"}
        return {"postgresql"}
    return set(services)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _result(
    service: str,
    status: str,
    *,
    latency_ms: float | None = None,
    detail: str = "",
    source: str = "probe",
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "service": service,
        "status": status,
        "latency_ms": latency_ms,
        "detail": detail,
        "source": source,
        "checked_at": _now_iso(),
    }
    if extras:
        payload.update(extras)
    return payload


async def _http_get(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, Any, float, str | None]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            latency = (time.perf_counter() - started) * 1000
            body: Any
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
            else:
                body = resp.text
            return resp.status_code, body, latency, None
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        return None, None, latency, str(exc)


async def _tcp_reachable(host: str, port: int, timeout: float) -> tuple[bool, float, str | None]:
    started = time.perf_counter()
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, (time.perf_counter() - started) * 1000, None
    except Exception as exc:
        return False, (time.perf_counter() - started) * 1000, str(exc)


async def probe_postgresql_host(host: str, port: int, timeout: float) -> dict[str, Any]:
    ok, latency, err = await _tcp_reachable(host, port, timeout)
    if ok:
        return _result("postgresql", "up", latency_ms=latency, detail=f"TCP {host}:{port} open")
    return _result("postgresql", "down", latency_ms=latency, detail=err or "unreachable")


async def probe_postgresql(instance: Instance, timeout: float) -> dict[str, Any]:
    return await probe_postgresql_host(instance.host, instance.port, timeout)


async def probe_sqlserver_host(host: str, port: int, timeout: float) -> dict[str, Any]:
    ok, latency, err = await _tcp_reachable(host, port, timeout)
    if ok:
        return _result("sqlserver", "up", latency_ms=latency, detail=f"TCP {host}:{port} open")
    return _result("sqlserver", "down", latency_ms=latency, detail=err or "unreachable")


async def probe_alwayson_host(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Lightweight reachability signal only — full AG sync-state/role detail comes from the
    DMV-based GET /groups/{id}/alwayson endpoint, not this generic multi-node probe."""
    ok, latency, err = await _tcp_reachable(host, port, timeout)
    if ok:
        return _result("alwayson", "up", latency_ms=latency, detail=f"AG replica endpoint {host}:{port} açık")
    return _result("alwayson", "down", latency_ms=latency, detail=err or "unreachable")


async def probe_windows_cluster_host(timeout: float) -> dict[str, Any]:
    """No native WSFC (Windows Server Failover Clustering) probing without a host agent —
    merge_agent_into_services() will override this if the node's server has one configured
    and it reports a "windows_cluster" service key."""
    return _result("windows_cluster", "skipped", detail="Windows cluster durumu için host agent gerekli")


async def probe_patroni_host(
    host: str, opts: dict[str, Any], timeout: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    port = int(opts.get("patroni_port") or 8008)
    scheme = "https" if opts.get("patroni_tls") else "http"
    base = f"{scheme}://{host}:{port}"
    status_code, body, latency, err = await _http_get(f"{base}/patroni", timeout=timeout)
    cluster_summary = None

    if err or status_code is None:
        # Fallback health endpoint
        status_code2, body2, latency2, err2 = await _http_get(f"{base}/health", timeout=timeout)
        if err2 or status_code2 is None:
            return _result("patroni", "down", latency_ms=latency, detail=err or err2 or "unreachable"), None
        ok = status_code2 == 200
        return (
            _result(
                "patroni",
                "up" if ok else "down",
                latency_ms=latency2,
                detail=f"/health HTTP {status_code2}",
                extras={"role": None, "state": None},
            ),
            None,
        )

    role = None
    state = None
    if isinstance(body, dict):
        role = body.get("role")
        state = body.get("state")
        extras = {"role": role, "state": state, "patroni_version": (body.get("patroni") or {}).get("version")}
    else:
        extras = {"role": None, "state": None}

    ok = status_code == 200
    result = _result(
        "patroni",
        "up" if ok else "down",
        latency_ms=latency,
        detail=f"/patroni HTTP {status_code}; role={role}; state={state}",
        extras=extras,
    )

    # Cluster member summary
    c_code, c_body, _, c_err = await _http_get(f"{base}/cluster", timeout=timeout)
    if not c_err and c_code == 200 and isinstance(c_body, dict):
        members = c_body.get("members") or []
        leader = None
        member_rows = []
        for m in members:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            m_role = m.get("role")
            m_state = m.get("state")
            m_host = m.get("host")
            m_lag = m.get("lag") if isinstance(m.get("lag"), (int, float)) else None
            member_rows.append(
                {"name": name, "role": m_role, "state": m_state, "host": m_host, "lag": m_lag}
            )
            if m_role in {"leader", "master", "primary"}:
                leader = name
        cluster_summary = {
            "leader": leader,
            "members": member_rows,
            "member_count": len(member_rows),
            "has_leader": leader is not None,
        }
    return result, cluster_summary


async def probe_patroni(
    instance: Instance, opts: dict[str, Any], timeout: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return await probe_patroni_host(instance.host, opts, timeout)


async def probe_etcd_host(host: str, opts: dict[str, Any], timeout: float) -> dict[str, Any]:
    port = int(opts.get("etcd_port") or 2379)
    url = f"http://{host}:{port}/health"
    status_code, body, latency, err = await _http_get(url, timeout=timeout)
    if err or status_code is None:
        return _result("etcd", "down", latency_ms=latency, detail=err or "unreachable")
    healthy = False
    if isinstance(body, dict):
        healthy = str(body.get("health", "")).lower() == "true"
    elif isinstance(body, str):
        healthy = "true" in body.lower()
    if status_code == 200 and healthy:
        return _result("etcd", "up", latency_ms=latency, detail="etcd health=true")
    return _result("etcd", "down", latency_ms=latency, detail=f"HTTP {status_code}; body={body!r}"[:240])


async def probe_etcd(instance: Instance, opts: dict[str, Any], timeout: float) -> dict[str, Any]:
    return await probe_etcd_host(instance.host, opts, timeout)


async def probe_haproxy_host(host: str, opts: dict[str, Any], timeout: float) -> dict[str, Any]:
    port = int(opts.get("haproxy_stats_port") or 8404)
    path = str(opts.get("haproxy_stats_path") or "/stats;csv")
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{host}:{port}{path}"
    status_code, body, latency, err = await _http_get(url, timeout=timeout)
    if err or status_code is None:
        return _result("haproxy", "down", latency_ms=latency, detail=err or "unreachable")
    if status_code != 200:
        return _result("haproxy", "down", latency_ms=latency, detail=f"HTTP {status_code}")

    up_backends = 0
    down_backends = 0
    if isinstance(body, str) and body:
        lines = body.strip().splitlines()
        # Skip header if present
        for line in (lines[1:] if lines and line_is_csv_header(lines[0]) else lines):
            cols = line.split(",")
            if len(cols) < 18:
                continue
            # pxname,svname,...,status at index 17 typically
            svname = cols[1] if len(cols) > 1 else ""
            status = cols[17] if len(cols) > 17 else ""
            if svname in {"BACKEND", "FRONTEND"}:
                continue
            if status.upper() == "UP":
                up_backends += 1
            elif status.upper() in {"DOWN", "MAINT", "DRAIN"}:
                down_backends += 1

    detail = f"stats OK; UP={up_backends} DOWN={down_backends}"
    status = "up" if up_backends > 0 or (up_backends == 0 and down_backends == 0) else "down"
    # If CSV parsed and all backends down → down
    if up_backends == 0 and down_backends > 0:
        status = "down"
    return _result(
        "haproxy",
        status,
        latency_ms=latency,
        detail=detail,
        extras={"up_backends": up_backends, "down_backends": down_backends},
    )


def line_is_csv_header(line: str) -> bool:
    return line.lower().startswith("#") or "pxname" in line.lower()


async def probe_haproxy(instance: Instance, opts: dict[str, Any], timeout: float) -> dict[str, Any]:
    return await probe_haproxy_host(instance.host, opts, timeout)


async def probe_keepalived_host(vip: str | None, port: int, timeout: float) -> dict[str, Any]:
    if not vip:
        return _result("keepalived", "unknown", detail="keepalived_vip tanımlı değil")
    # Prefer TCP to the database port on the VIP; also try patroni port as secondary signal.
    ok, latency, err = await _tcp_reachable(str(vip), port, timeout)
    if ok:
        return _result(
            "keepalived",
            "up",
            latency_ms=latency,
            detail=f"VIP {vip}:{port} reachable",
            extras={"vip": vip, "vip_owner_local": None},
        )
    # Fallback: DNS/socket resolution only
    try:
        socket.getaddrinfo(str(vip), None)
        return _result(
            "keepalived",
            "down",
            latency_ms=latency,
            detail=f"VIP resolves but port closed: {err}",
            extras={"vip": vip},
        )
    except Exception:
        return _result("keepalived", "down", latency_ms=latency, detail=err or "VIP unreachable", extras={"vip": vip})


async def probe_keepalived(instance: Instance, opts: dict[str, Any], timeout: float) -> dict[str, Any]:
    return await probe_keepalived_host(opts.get("keepalived_vip"), instance.port, timeout)


async def fetch_agent_snapshot(opts: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    agent_url = opts.get("agent_url")
    if not agent_url:
        return None
    token = opts.get("agent_token") or ""
    headers = {"X-Agent-Token": str(token)} if token else {}
    base = str(agent_url).rstrip("/") + "/"

    services_url = urljoin(base, "v1/services")
    keepalived_url = urljoin(base, "v1/keepalived")
    status, body, _, err = await _http_get(services_url, timeout=timeout, headers=headers)
    if err or status != 200 or not isinstance(body, dict):
        logger.warning("agent services failed: %s %s", status, err)
        return None

    kv_status, kv_body, _, _ = await _http_get(keepalived_url, timeout=timeout, headers=headers)
    keepalived = kv_body if kv_status == 200 and isinstance(kv_body, dict) else {}
    return {"services": body.get("services") or {}, "keepalived": keepalived, "agent_ok": True}


async def fetch_agent_logs(
    opts: dict[str, Any],
    service: str,
    lines: int = 100,
    timeout: float = 5.0,
) -> dict[str, Any]:
    agent_url = opts.get("agent_url")
    if not agent_url:
        raise ValueError("agent_url tanımlı değil")
    token = opts.get("agent_token") or ""
    headers = {"X-Agent-Token": str(token)} if token else {}
    base = str(agent_url).rstrip("/") + "/"
    url = urljoin(base, f"v1/logs?service={service}&lines={lines}")
    status, body, _, err = await _http_get(url, timeout=timeout, headers=headers)
    if err or status != 200:
        raise RuntimeError(err or f"agent logs HTTP {status}")
    if not isinstance(body, dict):
        raise RuntimeError("invalid agent log response")
    return body


def merge_agent_into_services(
    services: list[dict[str, Any]],
    agent: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not agent:
        return services
    agent_services = agent.get("services") or {}
    kv = agent.get("keepalived") or {}
    merged: list[dict[str, Any]] = []
    for item in services:
        name = item["service"]
        agent_state = agent_services.get(name)
        if agent_state:
            active = str(agent_state.get("active") or "").lower()
            status = "up" if active == "active" else "down" if active else item["status"]
            detail = f"systemd={active}; {item.get('detail') or ''}".strip("; ")
            item = {
                **item,
                "status": status,
                "detail": detail,
                "source": "agent+probe" if item.get("source") == "probe" else "agent",
                "systemd_active": active,
            }
        if name == "keepalived" and kv:
            item = {
                **item,
                "vip_owner_local": kv.get("holds_vip"),
                "detail": (
                    f"{item.get('detail')}; vip_owner={kv.get('holds_vip')}; "
                    f"addresses={','.join(kv.get('addresses') or [])}"
                )[:300],
                "source": "agent+probe",
            }
            if kv.get("holds_vip") is True:
                item["status"] = "up"
        merged.append(item)
    return merged


async def collect_cluster_health(instance: Instance) -> dict[str, Any]:
    opts = _opts(instance)
    timeout = float(opts.get("probe_timeout_sec") or 3)
    enabled = _services(instance)

    tasks: dict[str, Any] = {}
    if "postgresql" in enabled:
        tasks["postgresql"] = asyncio.create_task(probe_postgresql(instance, timeout))
    if "patroni" in enabled:
        tasks["patroni"] = asyncio.create_task(probe_patroni(instance, opts, timeout))
    if "etcd" in enabled:
        tasks["etcd"] = asyncio.create_task(probe_etcd(instance, opts, timeout))
    if "haproxy" in enabled:
        tasks["haproxy"] = asyncio.create_task(probe_haproxy(instance, opts, timeout))
    if "keepalived" in enabled:
        tasks["keepalived"] = asyncio.create_task(probe_keepalived(instance, opts, timeout))

    agent_task = asyncio.create_task(fetch_agent_snapshot(opts, timeout))

    results: list[dict[str, Any]] = []
    cluster_summary = None

    for name, task in tasks.items():
        try:
            value = await task
            if name == "patroni":
                result, cluster_summary = value
                results.append(result)
            else:
                results.append(value)
        except Exception as exc:
            results.append(_result(name, "unknown", detail=str(exc)))

    # skipped services that exist in canonical list but not enabled
    for svc in ("etcd", "patroni", "postgresql", "keepalived", "haproxy"):
        if svc not in enabled:
            results.append(_result(svc, "skipped", detail="services listesinde yok"))

    # stable order
    order = {"patroni": 0, "etcd": 1, "postgresql": 2, "keepalived": 3, "haproxy": 4}
    results.sort(key=lambda r: order.get(r["service"], 99))

    agent = None
    try:
        agent = await agent_task
    except Exception as exc:
        logger.debug("agent snapshot error: %s", exc)

    results = merge_agent_into_services(results, agent)

    down = sum(1 for r in results if r["status"] == "down")
    up = sum(1 for r in results if r["status"] == "up")
    unknown = sum(1 for r in results if r["status"] == "unknown")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    overall = "healthy"
    if down > 0:
        overall = "critical"
    elif unknown > 0:
        overall = "warning"
    elif up == 0 and skipped > 0:
        overall = "unknown"

    if cluster_summary is not None and not cluster_summary.get("has_leader"):
        overall = "critical"

    return {
        "instance_id": instance.id,
        "cluster_name": instance.cluster_name,
        "overall": overall,
        "checked_at": _now_iso(),
        "services": results,
        "cluster": cluster_summary,
        "agent": {
            "configured": bool(opts.get("agent_url")),
            "reachable": bool(agent and agent.get("agent_ok")),
            "url": opts.get("agent_url"),
        },
        "totals": {"up": up, "down": down, "unknown": unknown, "skipped": skipped},
    }


def cluster_health_metric_flags(report: dict[str, Any]) -> dict[str, float]:
    """Numeric flags for alert_engine / metrics_json."""
    flags: dict[str, float] = {
        "cluster_services_down": float(report.get("totals", {}).get("down") or 0),
        "cluster_has_leader": 1.0,
        "patroni_down": 0.0,
        "etcd_down": 0.0,
        "haproxy_down": 0.0,
        "keepalived_vip_down": 0.0,
        "postgresql_service_down": 0.0,
    }
    cluster = report.get("cluster") or {}
    if cluster and not cluster.get("has_leader", True):
        flags["cluster_has_leader"] = 0.0
    for svc in report.get("services") or []:
        name = svc.get("service")
        status = svc.get("status")
        if status != "down":
            continue
        if name == "patroni":
            flags["patroni_down"] = 1.0
        elif name == "etcd":
            flags["etcd_down"] = 1.0
        elif name == "haproxy":
            flags["haproxy_down"] = 1.0
        elif name == "keepalived":
            flags["keepalived_vip_down"] = 1.0
        elif name == "postgresql":
            flags["postgresql_service_down"] = 1.0
    return flags


def compact_cluster_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "overall": report.get("overall"),
        "checked_at": report.get("checked_at"),
        "totals": report.get("totals"),
        "cluster": report.get("cluster"),
        "services": [
            {
                "service": s.get("service"),
                "status": s.get("status"),
                "detail": s.get("detail"),
                "role": s.get("role"),
                "source": s.get("source"),
            }
            for s in report.get("services") or []
        ],
        "agent_reachable": bool((report.get("agent") or {}).get("reachable")),
    }


def _node_opts(node: Node) -> dict[str, Any]:
    raw = dict(DEFAULT_OPTIONS)
    raw.update(node.options or {})
    return raw


def _node_services(group: DatabaseGroup, node: Node) -> set[str]:
    """Which services to probe for one node — engine- and topology-aware.

    - topology=patroni (always engine=postgresql): full Patroni/etcd/keepalived/haproxy stack.
    - topology=alwayson (always engine=sqlserver): SQL Server + Always On + Windows cluster —
      never the Postgres stack.
    - topology=standalone: no "cluster service" stack at all, just a single engine-correct
      connectivity check (this used to always be a literal "postgresql" TCP probe regardless
      of engine — the root cause of a SQL Server standalone group reporting "PostgreSQL down").
    """
    raw = node.options or {}
    override = raw.get("services")
    if override:
        return set(override)
    if group.topology == "patroni":
        return {"etcd", "patroni", "postgresql", "keepalived", "haproxy"}
    if group.topology == "alwayson":
        return {"sqlserver", "alwayson", "windows_cluster"}
    return {_ENGINE_SERVICE_NAME.get(group.engine, group.engine)}


async def probe_node(group: DatabaseGroup, node: Node, timeout_default: float) -> dict[str, Any]:
    """Probe every enabled service for one Node, plus its Server's optional host-agent
    snapshot. Host/site/agent connection info all come from node.server now (Faz 8 test turu
    İŞ 1) — a node without a server assigned is reported as unknown rather than crashing."""
    server = node.server
    opts = _node_opts(node)
    timeout = float(opts.get("probe_timeout_sec") or timeout_default)

    if server is None:
        return {
            "node_id": node.id,
            "node_name": node.name,
            "site": "primary",
            "role_hint": node.role_hint,
            "services": [_result("server", "unknown", detail="Düğüme bağlı bir sunucu yok")],
            "cluster_summary": None,
            "agent": {"configured": False, "reachable": False, "url": None},
        }

    enabled = _node_services(group, node)

    tasks: dict[str, Any] = {}
    if "postgresql" in enabled:
        tasks["postgresql"] = asyncio.create_task(probe_postgresql_host(server.host, node.port, timeout))
    if "sqlserver" in enabled:
        tasks["sqlserver"] = asyncio.create_task(probe_sqlserver_host(server.host, node.port, timeout))
    if "alwayson" in enabled:
        tasks["alwayson"] = asyncio.create_task(probe_alwayson_host(server.host, node.port, timeout))
    if "windows_cluster" in enabled:
        tasks["windows_cluster"] = asyncio.create_task(probe_windows_cluster_host(timeout))
    if "patroni" in enabled:
        tasks["patroni"] = asyncio.create_task(probe_patroni_host(server.host, opts, timeout))
    if "etcd" in enabled:
        tasks["etcd"] = asyncio.create_task(probe_etcd_host(server.host, opts, timeout))
    if "haproxy" in enabled:
        tasks["haproxy"] = asyncio.create_task(probe_haproxy_host(server.host, opts, timeout))
    if "keepalived" in enabled:
        tasks["keepalived"] = asyncio.create_task(
            probe_keepalived_host(opts.get("keepalived_vip"), node.port, timeout)
        )

    agent_opts = {"agent_url": server.agent_url, "agent_token": server.agent_token}
    agent_task = asyncio.create_task(fetch_agent_snapshot(agent_opts, timeout))

    results: list[dict[str, Any]] = []
    cluster_summary: dict[str, Any] | None = None
    for name, task in tasks.items():
        try:
            value = await task
            if name == "patroni":
                result, cluster_summary = value
                results.append(result)
            else:
                results.append(value)
        except Exception as exc:
            results.append(_result(name, "unknown", detail=str(exc)))

    order = {
        "patroni": 0, "etcd": 1, "postgresql": 2, "keepalived": 3, "haproxy": 4,
        "alwayson": 0, "windows_cluster": 1, "sqlserver": 2,
    }
    results.sort(key=lambda r: order.get(r["service"], 99))

    agent = None
    try:
        agent = await agent_task
    except Exception as exc:
        logger.debug("agent snapshot error for node %s: %s", node.id, exc)
    results = merge_agent_into_services(results, agent)

    return {
        "node_id": node.id,
        "node_name": node.name,
        "site": server.site,
        "role_hint": node.role_hint,
        "services": results,
        "cluster_summary": cluster_summary,
        "agent": {
            "configured": bool(server.agent_url),
            "reachable": bool(agent and agent.get("agent_ok")),
            "url": server.agent_url,
        },
    }


async def collect_group_health(group: DatabaseGroup, nodes: list[Node]) -> dict[str, Any]:
    """Multi-node health for a DatabaseGroup: per-node probes + etcd quorum + split-brain
    detection. etcd/keepalived-based checks naturally no-op for non-Patroni groups since
    _node_services() never includes those services outside topology=patroni."""
    timeout_default = 3.0
    node_reports = list(await asyncio.gather(*(probe_node(group, node, timeout_default) for node in nodes)))

    cluster_summary = next((r["cluster_summary"] for r in node_reports if r.get("cluster_summary")), None)
    for r in node_reports:
        r.pop("cluster_summary", None)

    etcd_statuses = [s for r in node_reports for s in r["services"] if s["service"] == "etcd"]
    etcd_total = len(etcd_statuses)
    etcd_up = sum(1 for s in etcd_statuses if s["status"] == "up")
    quorum_size = etcd_total // 2 + 1 if etcd_total else 0
    etcd_quorum = {
        "total": etcd_total,
        "up": etcd_up,
        "quorum_size": quorum_size,
        "has_quorum": (etcd_up >= quorum_size) if etcd_total else True,
    }

    vip_owner_nodes = [
        r["node_name"]
        for r in node_reports
        for s in r["services"]
        if s["service"] == "keepalived" and s.get("vip_owner_local") is True
    ]
    split_brain = len(vip_owner_nodes) > 1

    # The "is this node down" service depends on the group's engine — used to always be a
    # hardcoded "postgresql" check, which silently never matched (and so never flagged a down
    # node) for sqlserver/mongodb groups.
    primary_service = _ENGINE_SERVICE_NAME.get(group.engine, group.engine)
    down_nodes = [
        {"node_name": r["node_name"], "site": r["site"]}
        for r in node_reports
        if any(s["service"] == primary_service and s["status"] == "down" for s in r["services"])
    ]

    up = sum(1 for r in node_reports for s in r["services"] if s["status"] == "up")
    down = sum(1 for r in node_reports for s in r["services"] if s["status"] == "down")
    unknown = sum(1 for r in node_reports for s in r["services"] if s["status"] == "unknown")
    skipped = sum(1 for r in node_reports for s in r["services"] if s["status"] == "skipped")

    overall = "healthy"
    if unknown:
        overall = "warning"
    if down_nodes or split_brain or not etcd_quorum["has_quorum"]:
        overall = "critical"
    if cluster_summary is not None and not cluster_summary.get("has_leader"):
        overall = "critical"

    return {
        "group_id": group.id,
        "group_name": group.name,
        "topology": group.topology,
        "overall": overall,
        "checked_at": _now_iso(),
        "nodes": node_reports,
        "cluster": cluster_summary,
        "etcd_quorum": etcd_quorum,
        "split_brain": split_brain,
        "split_brain_nodes": vip_owner_nodes,
        "down_nodes": down_nodes,
        "totals": {"up": up, "down": down, "unknown": unknown, "skipped": skipped},
    }


def group_health_metric_flags(report: dict[str, Any]) -> dict[str, float]:
    """Numeric flags for alert_engine — group-level counterpart of cluster_health_metric_flags."""
    max_lag = 0.0
    cluster = report.get("cluster") or {}
    for member in cluster.get("members") or []:
        lag = member.get("lag")
        if isinstance(lag, (int, float)):
            max_lag = max(max_lag, float(lag))

    return {
        "replication_lag_bytes": max_lag,
        "etcd_quorum_lost": 0.0 if (report.get("etcd_quorum") or {}).get("has_quorum", True) else 1.0,
        "split_brain": 1.0 if report.get("split_brain") else 0.0,
        "node_down": float(len(report.get("down_nodes") or [])),
    }
