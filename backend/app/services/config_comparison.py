"""Düğümler arası yapılandırma karşılaştırması (Faz 28 İŞ 4).

İki yoldan besleniyor ve ikisi de **aynı** karşılaştırma fonksiyonunu kullanıyor
(`domain/config_drift.compare_nodes`):

* **Canlı** — grup detay sekmesi için, düğümlere o an bağlanarak.
* **Saklanmış** — sağlık raporu için, günlük `DailyStateSnapshot` fotoğraflarından. Rapor
  canlı probe yapmıyor (bkz. services/health_report.py) ve bu kural burada da geçerli.

Ayrı iki karşılaştırma yazmak, sekmede "sapma yok" derken raporda "3 sapma" demek olurdu.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.config_drift import (
    TRACE_FLAG_CONSEQUENCE,
    DriftClass,
    DriftRow,
    compare_nodes,
    compared_parameters,
)
from app.models import DailyStateSnapshot, DatabaseGroup, Instance
from app.services.credentials import decrypt_secret

logger = logging.getLogger(__name__)

#: Snapshot türü. `parameters` zaten kullanılıyor (baseline denetimi); karşılaştırma daha
#: geniş bir parametre kümesine ihtiyaç duyduğu için ayrı bir tür.
SNAPSHOT_KIND = "config"


async def fetch_postgresql_settings(instance: Instance) -> dict[str, str | None]:
    """Karşılaştırma listesindeki parametreleri okur."""
    import asyncpg

    names = [p.name for p in compared_parameters("postgresql")]
    ssl_mode = (instance.options or {}).get("ssl_mode")
    conn = await asyncpg.connect(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        user=instance.username,
        password=decrypt_secret(instance.password),
        timeout=10,
        ssl=True if ssl_mode == "require" else None,
        statement_cache_size=0,
    )
    try:
        await conn.execute("SET statement_timeout = '5000ms'")
        rows = await conn.fetch(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY($1)", names
        )
    finally:
        await conn.close()
    # Birim dahil ediliyor: "128" ile "128MB" farkı, birim olmadan görünmez olurdu.
    return {
        row["name"]: (f"{row['setting']}{row['unit']}" if row["unit"] else row["setting"])
        for row in rows
    }


_SQLSERVER_CONFIG_SQL = """
SELECT name, CAST(value_in_use AS NVARCHAR(64)) AS value_in_use
FROM sys.configurations
"""

#: Açık trace flag'ler. `DBCC TRACESTATUS(-1)` global olanları döndürüyor ve `WITH NO_INFOMSGS`
#: çıktıyı tabloya indiriyor.
_SQLSERVER_TRACE_SQL = "DBCC TRACESTATUS(-1) WITH NO_INFOMSGS"


async def fetch_sqlserver_settings(instance: Instance) -> dict[str, str | None]:
    import aioodbc

    from app.collectors.base import ConnectionTarget
    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    conn = await aioodbc.connect(
        dsn=build_odbc_connection_string(target), timeout=10, autocommit=True
    )
    settings: dict[str, str | None] = {}
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET LOCK_TIMEOUT 5000")
        async with conn.cursor() as cur:
            await cur.execute(_SQLSERVER_CONFIG_SQL)
            for name, value in await cur.fetchall():
                settings[str(name).strip()] = str(value)
        try:
            async with conn.cursor() as cur:
                await cur.execute(_SQLSERVER_TRACE_SQL)
                rows = await cur.fetchall()
            # TRACESTATUS: (TraceFlag, Status, Global, Session)
            flags = sorted(str(int(row[0])) for row in rows if row and int(row[1] or 0) == 1)
            settings["_trace_flags"] = ",".join(flags) if flags else "(yok)"
        except Exception as exc:
            # Trace flag okumak ayrı bir yetki gerektirebiliyor. Sessizce "(yok)" yazmak
            # YANLIŞ olurdu: kapalı olduğu izlenimi verirdi. Okunamadığı yazılıyor.
            logger.debug("trace flag okunamadı (%s): %s", instance.name, exc)
            settings["_trace_flags"] = None
    finally:
        await conn.close()
    return settings


async def collect_instance_config(instance: Instance) -> dict[str, Any]:
    """Bir instance'ın karşılaştırma parametreleri; hata da kaydediliyor."""
    try:
        if instance.engine == "postgresql":
            settings = await fetch_postgresql_settings(instance)
        elif instance.engine == "sqlserver":
            settings = await fetch_sqlserver_settings(instance)
        else:
            return {"error": f"{instance.engine} için yapılandırma karşılaştırması desteklenmiyor"}
    except Exception as exc:  # noqa: BLE001
        # Sessizce boş dönmek "hepsi aynı" izlenimi verirdi; sebep kaydediliyor.
        return {"error": str(exc)[:500]}
    return {"settings": settings, "collected_at": datetime.now(UTC).isoformat()}


def _trace_flag_row(node_settings: dict[str, dict[str, str | None]]) -> DriftRow | None:
    """Trace flag'ler `sys.configurations`'da görünmez ama planlayıcıyı kökten değiştirir."""
    values = {node: (settings or {}).get("_trace_flags") for node, settings in node_settings.items()}
    if all(v is None for v in values.values()):
        return None
    present = [v for v in values.values() if v is not None]
    return DriftRow(
        name="Trace flag'ler",
        drift_class=str(DriftClass.SHOULD_MATCH),
        consequence=TRACE_FLAG_CONSEQUENCE,
        values=values,
        diverged=len(set(present)) > 1,
        missing_nodes=[node for node, value in values.items() if value is None],
    )


def build_comparison(engine: str, node_settings: dict[str, dict[str, str | None]]) -> dict[str, Any]:
    """Karşılaştırma sonucunu üretir (canlı ve saklanmış yol için ORTAK)."""
    rows = compare_nodes(engine, node_settings)
    if engine == "sqlserver":
        trace_row = _trace_flag_row(node_settings)
        if trace_row is not None:
            rows.insert(0 if trace_row.diverged else len(rows), trace_row)

    diverged = [r for r in rows if r.diverged]
    critical = [r for r in diverged if r.severity == "critical"]
    warning = [r for r in diverged if r.severity == "warning"]
    return {
        "engine": engine,
        "nodes": list(node_settings),
        "rows": [r.to_dict() for r in rows],
        "diverged_count": len(diverged),
        "critical_count": len(critical),
        "warning_count": len(warning),
    }


async def compare_group_live(session: AsyncSession, group: DatabaseGroup) -> dict[str, Any]:
    """Gruptaki tüm veritabanlarına o an bağlanarak karşılaştırır."""
    instances = (
        await session.execute(
            select(Instance).where(Instance.group_id == group.id, Instance.enabled.is_(True))
        )
    ).scalars().all()
    if len(instances) < 2:
        # Tek düğümlü bir grupta "karşılaştırma" diye bir şey yok; boş tablo göstermek yerine
        # neden gösterilemediği söyleniyor.
        return {
            "engine": group.engine,
            "nodes": [i.name for i in instances],
            "rows": [],
            "diverged_count": 0,
            "critical_count": 0,
            "warning_count": 0,
            "unavailable_reason": (
                "Karşılaştırma için en az iki düğüm gerekiyor; bu grupta "
                f"{len(instances)} veritabanı tanımlı."
            ),
        }

    node_settings: dict[str, dict[str, str | None]] = {}
    errors: dict[str, str] = {}
    for instance in instances:
        payload = await collect_instance_config(instance)
        if payload.get("error"):
            errors[instance.name] = payload["error"]
            node_settings[instance.name] = {}
        else:
            node_settings[instance.name] = payload["settings"]

    result = build_comparison(group.engine, node_settings)
    result["errors"] = errors
    result["checked_at"] = datetime.now(UTC).isoformat()
    return result


async def compare_group_from_snapshots(
    session: AsyncSession, group: DatabaseGroup, instances: list[Instance], day: date | None = None
) -> dict[str, Any]:
    """Saklanmış günlük fotoğraflardan karşılaştırır — rapor bölümü için (canlı probe yok)."""
    instance_ids = [i.id for i in instances]
    if len(instance_ids) < 2:
        return {
            "engine": group.engine,
            "nodes": [i.name for i in instances],
            "rows": [],
            "diverged_count": 0,
            "critical_count": 0,
            "warning_count": 0,
            "unavailable_reason": "Karşılaştırma için en az iki düğüm gerekiyor.",
        }

    stmt = (
        select(DailyStateSnapshot)
        .where(
            DailyStateSnapshot.instance_id.in_(instance_ids),
            DailyStateSnapshot.kind == SNAPSHOT_KIND,
        )
        .order_by(DailyStateSnapshot.day.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()

    # Instance başına EN YENİ fotoğraf. Farklı günlerin fotoğraflarını karşılaştırmak
    # yanıltıcı olurdu ama bir düğümün fotoğrafı bir gün eskiyse onu tamamen dışarıda
    # bırakmak da sapmayı gizlerdi; hangi güne ait olduğu sonuçta taşınıyor.
    latest: dict[int, DailyStateSnapshot] = {}
    for row in rows:
        if day is not None and row.day != day:
            continue
        latest.setdefault(row.instance_id, row)

    node_settings: dict[str, dict[str, str | None]] = {}
    errors: dict[str, str] = {}
    snapshot_days: dict[str, str] = {}
    for instance in instances:
        snapshot = latest.get(instance.id)
        if snapshot is None:
            errors[instance.name] = "Bu düğüm için yapılandırma fotoğrafı yok."
            node_settings[instance.name] = {}
            continue
        payload = snapshot.payload or {}
        snapshot_days[instance.name] = snapshot.day.isoformat()
        if payload.get("error"):
            errors[instance.name] = str(payload["error"])
            node_settings[instance.name] = {}
        else:
            node_settings[instance.name] = payload.get("settings") or {}

    result = build_comparison(group.engine, node_settings)
    result["errors"] = errors
    result["snapshot_days"] = snapshot_days
    return result
