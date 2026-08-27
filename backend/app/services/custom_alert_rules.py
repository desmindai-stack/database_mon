from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.collectors.base import ConnectionTarget, classify_connection_error
from app.models import AlertEvent, AlertRule, Instance, Node
from app.services.alert_engine import _compare
from app.services.credentials import decrypt_secret

logger = logging.getLogger(__name__)

# Hard cap on how long a custom rule's query is allowed to run — enforced both as a
# driver-level query timeout where the driver supports it, and as an outer asyncio.wait_for
# safety net (connection close aborts the in-flight query server-side either way).
QUERY_TIMEOUT_SECONDS = 10

_STATEMENT_KEYWORD = re.compile(r"(?is)^\s*(SELECT|WITH)\b")
# Defense in depth beyond the SELECT/WITH prefix check: PostgreSQL allows data-modifying CTEs
# (`WITH x AS (DELETE FROM t RETURNING *) SELECT count(*) FROM x`) which are syntactically a
# SELECT but mutate data — a prefix check alone does not catch this. Scan for these keywords
# anywhere in the statement, not just at the start.
_DENYLISTED_KEYWORDS = re.compile(
    r"(?is)\b(INSERT|UPDATE|DELETE|MERGE|EXEC|EXECUTE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"CALL|VACUUM|COPY|INTO\s+OUTFILE)\b"
)


def validate_readonly_sql(query: str) -> None:
    """Best-effort SELECT-only validation for custom alert rule queries.

    This is one of two layers — the real enforcement for PostgreSQL targets is a genuine
    READ ONLY transaction (see _run_query), which PostgreSQL itself rejects any write against,
    including the data-modifying-CTE trick this regex can't fully rule out on its own. For
    SQL Server there is no equivalent ad-hoc read-only transaction primitive available over a
    normal login, so this validation is the primary defense there — see SORULAR.md.
    """
    stripped = (query or "").strip()
    if not stripped:
        raise ValueError("Sorgu boş olamaz")
    body = stripped.rstrip(";").strip()
    if ";" in body:
        raise ValueError("Sorgu birden fazla ifade içeremez (';' ile ayrılmış ek komutlar yasak)")
    if not _STATEMENT_KEYWORD.match(body):
        raise ValueError("Sadece SELECT veya WITH ile başlayan salt-okunur sorgulara izin veriliyor")
    if _DENYLISTED_KEYWORDS.search(body):
        raise ValueError("Sorgu izin verilmeyen bir anahtar kelime içeriyor (veri/şema değiştiren komutlar yasak)")


async def _resolve_target_instance(
    session: AsyncSession, instance_id: int | None, group_id: int | None
) -> Instance | None:
    if instance_id:
        return await session.get(Instance, instance_id)
    if group_id:
        nodes = (
            await session.execute(
                select(Node).options(selectinload(Node.instance)).where(Node.group_id == group_id)
            )
        ).scalars().all()
        if not nodes:
            return None
        target_node = next((n for n in nodes if n.role_hint == "primary"), nodes[0])
        return target_node.instance
    return None


async def test_custom_query(
    session: AsyncSession, *, sql_query: str, instance_id: int | None, group_id: int | None
) -> tuple[bool, str, float | None]:
    """Runs a candidate custom-rule query against its target right now, outside of any saved
    AlertRule row — backs the wizard's "sorguyu test et" button (Faz 15 İŞ 7) so a rule can be
    validated before it's saved, not just after the fact via the scheduler tick."""
    try:
        validate_readonly_sql(sql_query)
    except ValueError as exc:
        return False, str(exc), None

    instance = await _resolve_target_instance(session, instance_id, group_id)
    if instance is None:
        return False, "Hedef instance bulunamadı (grup için düğüm/instance eksik olabilir)", None

    try:
        value = await asyncio.wait_for(_run_query(instance, sql_query), timeout=QUERY_TIMEOUT_SECONDS + 5)
    except Exception as exc:
        return False, classify_connection_error(exc), None

    if value is None:
        return True, "Sorgu çalıştı, sonuç NULL döndü", None
    return True, f"Sorgu başarılı — sonuç: {value}", value


async def _run_query(instance: Instance, query: str) -> float | None:
    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )

    if instance.engine == "postgresql":
        import asyncpg

        conn = await asyncpg.connect(
            host=target.host,
            port=target.port,
            database=target.database,
            user=target.username,
            password=target.password,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        try:
            await conn.execute(f"SET statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'")
            async with conn.transaction(readonly=True):
                value = await conn.fetchval(query, timeout=QUERY_TIMEOUT_SECONDS)
            return float(value) if value is not None else None
        finally:
            await conn.close()

    if instance.engine == "sqlserver":
        import aioodbc

        from app.collectors.sqlserver_mongodb import build_odbc_connection_string

        conn = await aioodbc.connect(
            dsn=build_odbc_connection_string(target), timeout=QUERY_TIMEOUT_SECONDS, autocommit=True
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute(query)
                row = await cur.fetchone()
                if not row or row[0] is None:
                    return None
                return float(row[0])
        finally:
            await conn.close()

    raise ValueError(f"Özel kural sorguları şu an bu engine için desteklenmiyor: {instance.engine}")


async def evaluate_custom_alert_rules(session: AsyncSession) -> None:
    """Runs every enabled custom rule whose own interval_seconds has elapsed since
    last_run_at. Called on a short fixed scheduler tick (see collectors/scheduler.py) — the
    per-rule cadence is self-managed here rather than via one APScheduler job per rule."""
    now = datetime.now(UTC)
    rules = (
        await session.execute(
            select(AlertRule).where(AlertRule.rule_type == "custom", AlertRule.enabled.is_(True))
        )
    ).scalars().all()

    for rule in rules:
        last_run = rule.last_run_at
        due = last_run is None or (now - last_run).total_seconds() >= rule.interval_seconds
        if not due:
            continue
        rule.last_run_at = now

        try:
            validate_readonly_sql(rule.sql_query or "")
            instance = await _resolve_target_instance(session, rule.instance_id, rule.group_id)
            if instance is None:
                logger.warning("custom alert rule %s (%s): hedef instance bulunamadı", rule.id, rule.name)
                continue

            value = await asyncio.wait_for(
                _run_query(instance, rule.sql_query or ""), timeout=QUERY_TIMEOUT_SECONDS + 5
            )
            if value is None:
                continue
            if not _compare(value, rule.operator, rule.threshold):
                continue

            existing = await session.execute(
                select(AlertEvent).where(AlertEvent.rule_id == rule.id, AlertEvent.resolved_at.is_(None))
            )
            if existing.scalar_one_or_none():
                continue

            session.add(
                AlertEvent(
                    rule_id=rule.id,
                    instance_id=rule.instance_id,
                    group_id=rule.group_id,
                    metric_value=value,
                    message=f"{rule.name}: özel sorgu sonucu {value} {rule.operator} {rule.threshold}",
                )
            )
        except Exception as exc:
            logger.warning("custom alert rule %s (%s) failed: %s", rule.id, rule.name, exc)

    await session.commit()
