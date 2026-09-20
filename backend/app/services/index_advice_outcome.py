"""Index önerisinin ÖLÇÜLMÜŞ etkisi: aynı sorgunun index kurulmadan önceki ve sonraki planı
(Faz 31 Commit 5).

## Neden

hypopg yokken fayda yüzdesi üretilmiyor: istatistik formülü gerçek sunucuda %88,6 derken
hypopg ile ölçülen %34'tü (Faz 31 Commit 4). Karar: asıl fayda yolu, index KURULDUKTAN sonra
aynı sorgunun önce/sonra EXPLAIN karşılaştırması — ölçülmüş ve kaynağı etiketli. hypopg varsa
öneri anındaki tahmin ek seçenek olarak kalıyor.

## Nasıl

1. Öneri üretilince (`register_outcomes`) her doğrulanmış öneri için sorgunun değerden bağımsız
   planı alınıyor (`generic_plan.explain_json` — sorgu ÇALIŞTIRILMIYOR) ve toplam planlayıcı
   maliyeti, plandaki index adları ve tablonun O ANDAKİ index'leri saklanıyor.
2. Zamanlayıcı turu (`outcome_tick`) hedefte, kayıtta olmayan ve anahtar kolonları öneriyle
   başlayan GEÇERLİ bir index arıyor. Bulunca aynı sorgunun planını yeniden alıyor.
3. Ölçülemiyorsa (yetki, bağlantı) sonuç boş değil "ölçülemedi" ve gerekçesi.

Karşılaştırılan şey planlayıcı MALİYETİ, çalışma süresi değil — etiket bunu söylüyor.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

#: Faz 31 Commit 9: tek sorgu için kayıtlı index önerisi ölçümü ve tur başına işlenen parti.
MAX_OUTCOMES_PER_QUERY = 50
TICK_BATCH = 200
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import classify_connection_error
from app.collectors.query_marker import connect_marked
from app.database import SessionLocal
from app.domain.engines import DatabaseEngine
from app.models import IndexAdviceOutcome, Instance
from app.services.generic_plan import explain_json
from app.services.plan_capture import fingerprint
from app.services.query_text_privacy import sanitize_stored_query

logger = logging.getLogger(__name__)

STATUS_WAITING = "waiting_for_index"
STATUS_MEASURED = "measured"
STATUS_NOT_MEASURABLE = "not_measurable"

SOURCE_LABEL = (
    "Ölçüldü: index kurulmadan önce ve kurulduktan sonra AYNI sorgunun planı (değerden bağımsız "
    "EXPLAIN — sorgu çalıştırılmadı). Karşılaştırılan, PostgreSQL planlayıcısının toplam maliyeti; "
    "çalışma süresi değil."
)
STATEMENT_TIMEOUT_MS = 8000


def _connect_kwargs(instance: Instance) -> dict[str, Any]:
    from app.services.collection import connection_target_for

    target = connection_target_for(instance)
    return {
        "host": target.host, "port": target.port, "database": target.database,
        "user": target.username, "password": target.password, "timeout": 10,
        "statement_cache_size": 0,
    }


def _plan_cost_and_indexes(plan: Any) -> tuple[float | None, list[str]]:
    root = plan[0] if isinstance(plan, list) and plan else plan
    top = (root or {}).get("Plan") or {}
    names: list[str] = []

    def walk(node: dict) -> None:
        if node.get("Index Name"):
            names.append(str(node["Index Name"]))
        for child in node.get("Plans") or []:
            walk(child)

    walk(top)
    cost = top.get("Total Cost")
    return (float(cost) if cost is not None else None), sorted(set(names))


def _measure_error(exc: Exception, table: str) -> str:
    text = str(exc)
    if "permission denied" in text.lower():
        return (
            f"Ölçülemedi: izleme kullanıcısının {table} tablosuna SELECT yetkisi yok; PostgreSQL "
            f"EXPLAIN için bunu istiyor. Çözüm: GRANT SELECT ON {table} TO <izleme_kullanıcısı>;"
        )
    return f"Ölçülemedi: plan alınamadı — {text[:300]}"


async def _explain(conn, query: str) -> tuple[float | None, list[str]]:
    return _plan_cost_and_indexes(await explain_json(conn, query))


async def register_outcomes(
    session: AsyncSession, instance: Instance, *, query: str, queryid: str | None, recommendations
) -> list[IndexAdviceOutcome]:
    """Doğrulanmış her öneri için "önce" ölçümü. Aynı sorgu + aynı DDL zaten kayıtlıysa önceki
    ölçüm KORUNUYOR: "önce" planı index kurulmadan alınmış olmalı."""
    recs = [r for r in recommendations if getattr(r, "verified", True)]
    if not recs or instance.engine != str(DatabaseEngine.POSTGRESQL):
        return []
    key = fingerprint(query)
    existing = {
        row.index_ddl: row
        for row in (
            await session.execute(
                select(IndexAdviceOutcome).where(
                    IndexAdviceOutcome.instance_id == instance.id, IndexAdviceOutcome.query_fingerprint == key
                ).limit(MAX_OUTCOMES_PER_QUERY)
            )
        ).scalars()
    }
    new = [r for r in recs if r.index_ddl not in existing]
    if not new:
        return list(existing.values())

    now = datetime.now(UTC)
    rows: list[IndexAdviceOutcome] = []
    conn = None
    connect_error = None
    try:
        conn = await connect_marked(**_connect_kwargs(instance))
        await conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
    except Exception as exc:  # noqa: BLE001
        connect_error = f"Ölçülemedi: sunucuya bağlanılamadı ({classify_connection_error(exc)})"
    try:
        for rec in new:
            table = f"{rec.schema_name}.{rec.table_name}"
            row = IndexAdviceOutcome(
                instance_id=instance.id, queryid=queryid, query_fingerprint=key,
                query_text=sanitize_stored_query(query, keep_values=True) or "",
                table_name=table, index_columns=list(rec.columns), index_ddl=rec.index_ddl,
                index_kind=rec.index_kind, registered_at=now, last_checked_at=now,
            )
            if connect_error:
                row.status, row.last_error = STATUS_NOT_MEASURABLE, connect_error
            else:
                try:
                    row.existing_indexes = await _index_names(conn, rec.schema_name, rec.table_name)
                    row.before_cost, row.before_indexes_used = await _explain(conn, query)
                    row.before_measured_at = now
                    row.status = STATUS_WAITING
                except Exception as exc:  # noqa: BLE001
                    row.status, row.last_error = STATUS_NOT_MEASURABLE, _measure_error(exc, table)
            session.add(row)
            rows.append(row)
    finally:
        if conn is not None:
            await conn.close()
    await session.flush()
    return rows + list(existing.values())


async def _index_names(conn, schema: str, table: str) -> list[str]:
    rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = $2", schema, table)
    return sorted(r["indexname"] for r in rows)


def _norm_key(text: str) -> str:
    return re.sub(r'[\s"()]', "", text or "").lower()


async def _matching_new_index(conn, row: IndexAdviceOutcome) -> str | None:
    """Kayıtta olmayan, geçerli ve anahtar kolonları öneriyle BAŞLAYAN index."""
    schema, _, table = row.table_name.partition(".")
    candidates = await conn.fetch(
        """
        SELECT ic.relname AS name,
               array(SELECT pg_get_indexdef(i.indexrelid, k, true)
                     FROM generate_series(1, i.indnkeyatts) k ORDER BY k) AS keys
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = $1 AND t.relname = $2 AND i.indisvalid
        """,
        schema, table,
    )
    wanted = [_norm_key(c) for c in row.index_columns]
    known = set(row.existing_indexes or [])
    for candidate in candidates:
        if candidate["name"] in known:
            continue
        keys = [_norm_key(k) for k in candidate["keys"]]
        if keys[: len(wanted)] == wanted:
            return candidate["name"]
    return None


async def check_outcome(conn, row: IndexAdviceOutcome, *, now: datetime) -> bool:
    """Index kurulduysa "sonra" ölçümünü yazar. Ölçüldüyse True."""
    row.last_checked_at = now
    try:
        name = await _matching_new_index(conn, row)
        if name is None:
            return False
        row.after_cost, row.after_indexes_used = await _explain(conn, row.query_text)
    except Exception as exc:  # noqa: BLE001 — bir sonraki turda tekrar
        row.last_error = _measure_error(exc, row.table_name)
        return False
    row.after_index_name = name
    row.after_measured_at = now
    row.status = STATUS_MEASURED
    row.last_error = None
    return True


async def outcome_tick() -> dict[str, int]:
    """Zamanlayıcı turu: index'i bekleyen kayıtlar. Veritabanı başına tek bağlantı."""
    totals = {"checked": 0, "measured": 0, "failed": 0}
    async with SessionLocal() as session:
        pending = (
            await session.execute(
                select(IndexAdviceOutcome, Instance)
                .join(Instance, Instance.id == IndexAdviceOutcome.instance_id)
                .where(
                    IndexAdviceOutcome.status == STATUS_WAITING,
                    Instance.enabled.is_(True),
                    Instance.engine == str(DatabaseEngine.POSTGRESQL),
                )
                .order_by(IndexAdviceOutcome.instance_id)
                .limit(TICK_BATCH)
            )
        ).all()
        by_instance: dict[int, tuple[Instance, list[IndexAdviceOutcome]]] = {}
        for row, instance in pending:
            by_instance.setdefault(instance.id, (instance, []))[1].append(row)
        now = datetime.now(UTC)
        for instance, rows in by_instance.values():
            try:
                conn = await connect_marked(**_connect_kwargs(instance))
            except Exception as exc:  # noqa: BLE001
                for row in rows:
                    row.last_checked_at = now
                    row.last_error = f"Ölçülemedi: sunucuya bağlanılamadı ({classify_connection_error(exc)})"
                totals["failed"] += len(rows)
                continue
            try:
                await conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
                for row in rows:
                    totals["checked"] += 1
                    if await check_outcome(conn, row, now=now):
                        totals["measured"] += 1
            finally:
                await conn.close()
        await session.commit()
    return totals


def outcome_payload(row: IndexAdviceOutcome) -> dict[str, Any]:
    change_pct = None
    if row.status == STATUS_MEASURED and row.before_cost and row.after_cost is not None:
        change_pct = round((row.before_cost - row.after_cost) / row.before_cost * 100, 1)
    return {
        "id": row.id,
        "queryid": row.queryid,
        "query_text": row.query_text,
        "table_name": row.table_name,
        "index_columns": list(row.index_columns or []),
        "index_ddl": row.index_ddl,
        "status": row.status,
        "source_label": SOURCE_LABEL,
        "before_cost": row.before_cost,
        "before_indexes_used": list(row.before_indexes_used or []),
        "before_measured_at": row.before_measured_at,
        "after_cost": row.after_cost,
        "after_indexes_used": list(row.after_indexes_used or []),
        "after_index_name": row.after_index_name,
        "after_uses_new_index": (row.after_index_name in (row.after_indexes_used or []))
        if row.status == STATUS_MEASURED else None,
        "after_measured_at": row.after_measured_at,
        "measured_cost_reduction_pct": change_pct,
        "note": row.last_error if row.status != STATUS_MEASURED else None,
        "registered_at": row.registered_at,
    }
