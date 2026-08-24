from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx

from app.models import DatabaseGroup, Node
from app.services.credentials import decrypt_node_options

logger = logging.getLogger(__name__)

_MEMORY_UNITS = {"B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "8kB": 8 * 1024, "16MB": 16 * 1024**2}
_TIME_UNITS = {"ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}

# Best-practice baseline for critical PostgreSQL parameters. Numeric bounds are
# conservative floors/ceilings meant to catch obviously risky configurations,
# not a full sizing calculator (that would need server RAM/CPU, which dbace
# doesn't collect today) — see SORULAR.md.
CRITICAL_PARAMETERS: dict[str, dict[str, Any]] = {
    "shared_buffers": {
        "category": "memory",
        "kind": "numeric",
        "unit_family": "memory",
        "min": 256 * 1024 * 1024,
        "severity_below": "medium",
        "recommendation": "En az 256MB (ideali: sunucu RAM'inin ~%25'i)",
    },
    "work_mem": {
        "category": "memory",
        "kind": "numeric",
        "unit_family": "memory",
        "min": 4 * 1024 * 1024,
        "severity_below": "low",
        "recommendation": "En az 4MB (yoğun sort/hash işlemler için daha yüksek gerekebilir)",
    },
    "max_connections": {
        "category": "connections",
        "kind": "numeric",
        "min": 20,
        "max": 500,
        "severity_below": "medium",
        "severity_above": "medium",
        "recommendation": "100-300 arası (pooler kullanmıyorsanız çok yüksek değerlerden kaçının)",
    },
    "wal_level": {
        "category": "replication",
        "kind": "enum",
        "allowed": {"replica", "logical"},
        "severity_invalid": "critical",
        "recommendation": "replica veya logical (Patroni/replikasyon için minimum 'replica')",
    },
    "max_wal_senders": {
        "category": "replication",
        "kind": "numeric",
        "min": 3,
        "severity_below": "high",
        "recommendation": "En az 3 (replika sayısı + yedekleme araçları kadar)",
    },
    "checkpoint_timeout": {
        "category": "checkpoint",
        "kind": "numeric",
        "unit_family": "time",
        "min": 60_000,
        "max": 1_800_000,
        "severity_below": "medium",
        "severity_above": "medium",
        "recommendation": "5-15 dakika arası (varsayılan 5dk çoğu yük için uygundur)",
    },
    "checkpoint_completion_target": {
        "category": "checkpoint",
        "kind": "numeric",
        "min": 0.5,
        "severity_below": "medium",
        "recommendation": "En az 0.7 (checkpoint I/O yükünü yaymak için)",
    },
    "autovacuum": {
        "category": "autovacuum",
        "kind": "enum",
        "allowed": {"on"},
        "severity_invalid": "critical",
        "recommendation": "her zaman 'on' (kapatmak bloat/wraparound riskini artırır)",
    },
    "autovacuum_max_workers": {
        "category": "autovacuum",
        "kind": "numeric",
        "min": 3,
        "severity_below": "medium",
        "recommendation": "En az 3",
    },
    "autovacuum_naptime": {
        "category": "autovacuum",
        "kind": "numeric",
        "unit_family": "time",
        "max": 300_000,
        "severity_above": "low",
        "recommendation": "5 dakikayı geçmemeli (varsayılan 1dk)",
    },
    "autovacuum_vacuum_scale_factor": {
        "category": "autovacuum",
        "kind": "numeric",
        "max": 0.2,
        "severity_above": "low",
        "recommendation": "Büyük tablolarda 0.1 veya altı önerilir (varsayılan 0.2)",
    },
    "autovacuum_analyze_scale_factor": {
        "category": "autovacuum",
        "kind": "numeric",
        "max": 0.1,
        "severity_above": "low",
        "recommendation": "Büyük tablolarda 0.05 veya altı önerilir (varsayılan 0.1)",
    },
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_value(setting: str, unit: str | None, unit_family: str | None) -> float | None:
    try:
        value = float(setting)
    except (TypeError, ValueError):
        return None
    if not unit:
        return value
    table = _MEMORY_UNITS if unit_family == "memory" else _TIME_UNITS if unit_family == "time" else None
    if table and unit in table:
        return value * table[unit]
    return value


def _evaluate_parameter(name: str, spec: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    category = spec["category"]
    recommendation = spec["recommendation"]

    if row is None:
        return {
            "name": name,
            "category": category,
            "current_value": None,
            "unit": None,
            "severity": "unknown",
            "recommendation": recommendation,
            "detail": "pg_settings içinde bulunamadı (sürüm farkı olabilir)",
        }

    current_display = f"{row['setting']}{row['unit'] or ''}"

    if spec["kind"] == "enum":
        allowed = spec["allowed"]
        if row["setting"] not in allowed:
            return {
                "name": name,
                "category": category,
                "current_value": current_display,
                "unit": row["unit"],
                "severity": spec["severity_invalid"],
                "recommendation": recommendation,
                "detail": f"Mevcut değer '{row['setting']}', beklenen: {', '.join(sorted(allowed))}",
            }
        return {
            "name": name,
            "category": category,
            "current_value": current_display,
            "unit": row["unit"],
            "severity": "ok",
            "recommendation": recommendation,
            "detail": "Baseline ile uyumlu",
        }

    value = _normalize_value(row["setting"], row["unit"], spec.get("unit_family"))
    if value is None:
        return {
            "name": name,
            "category": category,
            "current_value": current_display,
            "unit": row["unit"],
            "severity": "unknown",
            "recommendation": recommendation,
            "detail": "Değer sayısal olarak yorumlanamadı",
        }

    min_v = spec.get("min")
    max_v = spec.get("max")
    if min_v is not None and value < min_v:
        return {
            "name": name,
            "category": category,
            "current_value": current_display,
            "unit": row["unit"],
            "severity": spec.get("severity_below", "medium"),
            "recommendation": recommendation,
            "detail": f"Değer beklenen alt sınırın altında (min: {min_v})",
        }
    if max_v is not None and value > max_v:
        return {
            "name": name,
            "category": category,
            "current_value": current_display,
            "unit": row["unit"],
            "severity": spec.get("severity_above", "medium"),
            "recommendation": recommendation,
            "detail": f"Değer beklenen üst sınırın üzerinde (max: {max_v})",
        }
    return {
        "name": name,
        "category": category,
        "current_value": current_display,
        "unit": row["unit"],
        "severity": "ok",
        "recommendation": recommendation,
        "detail": "Baseline ile uyumlu",
    }


async def _pg_connect(node: Node) -> asyncpg.Connection:
    opts = decrypt_node_options(node.options) or {}
    username = opts.get("db_username")
    if not username:
        raise ValueError(
            f"Node '{node.name}' için node.options.db_username tanımlı değil (parametre denetimi bağlantı gerektirir)"
        )
    return await asyncpg.connect(
        host=node.host,
        port=node.port,
        database=opts.get("db_database", "postgres"),
        user=username,
        password=opts.get("db_password") or "",
        timeout=10,
    )


async def _fetch_pg_settings(node: Node) -> dict[str, dict[str, Any]]:
    conn = await _pg_connect(node)
    try:
        rows = await conn.fetch(
            "SELECT name, setting, unit, category, short_desc FROM pg_settings WHERE name = ANY($1)",
            list(CRITICAL_PARAMETERS.keys()),
        )
        return {row["name"]: dict(row) for row in rows}
    finally:
        await conn.close()


async def _fetch_patroni_config(node: Node) -> dict[str, Any] | None:
    opts = node.options or {}
    port = int(opts.get("patroni_port") or 8008)
    scheme = "https" if opts.get("patroni_tls") else "http"
    url = f"{scheme}://{node.host}:{port}/config"
    try:
        async with httpx.AsyncClient(timeout=5, verify=False) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"error": f"Patroni /config HTTP {resp.status_code}"}
            return resp.json()
    except Exception as exc:
        logger.debug("patroni /config fetch failed for node %s: %s", node.id, exc)
        return {"error": str(exc)}


def _select_target_node(nodes: list[Node]) -> Node:
    return next((n for n in nodes if n.role_hint == "primary"), nodes[0])


async def collect_parameter_audit(group: DatabaseGroup, nodes: list[Node]) -> dict[str, Any]:
    if group.engine != "postgresql":
        raise ValueError("Parametre denetimi şu anda yalnızca PostgreSQL grupları için destekleniyor")
    if not nodes:
        raise ValueError("Grupta düğüm yok")

    target = _select_target_node(nodes)
    settings_by_name = await _fetch_pg_settings(target)
    findings = [
        _evaluate_parameter(name, spec, settings_by_name.get(name)) for name, spec in CRITICAL_PARAMETERS.items()
    ]

    patroni_config = await _fetch_patroni_config(target) if group.topology == "patroni" else None

    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "ok": 0, "unknown": 0}
    for finding in findings:
        summary[finding["severity"]] = summary.get(finding["severity"], 0) + 1

    return {
        "group_id": group.id,
        "group_name": group.name,
        "node_id": target.id,
        "node_name": target.name,
        "checked_at": _now_iso(),
        "findings": findings,
        "patroni_config": patroni_config,
        "summary": summary,
    }
