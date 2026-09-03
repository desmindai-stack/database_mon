from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ConnectionTarget:
    host: str
    port: int
    database: str
    username: str
    password: str
    options: dict[str, Any] | None = None


# Hostname/port fragments strongly associated with a connection pooler sitting in front of the
# real PostgreSQL server. Best-effort only — an explicit options["uses_pooler"] always wins (see
# resolve_uses_pooler) since no heuristic can know about a self-hosted PgBouncer on a custom
# host/port.
_POOLER_HOST_HINTS = ("pooler", "pgbouncer")
# Supabase's pooled endpoint always listens on 6543 (its direct connection is 5432); 6432 is
# PgBouncer's own packaged default port, common in on-prem/enterprise deployments.
_POOLER_PORT_HINTS = {6432, 6543}


def detect_pooler(host: str, port: int) -> bool:
    """Best-effort auto-detection of a connection pooler (PgBouncer, Supabase's pooler) in
    front of a PostgreSQL target, from nothing but host/port — see resolve_uses_pooler for how
    this combines with an explicit per-instance override."""
    host_lower = (host or "").lower()
    if any(hint in host_lower for hint in _POOLER_HOST_HINTS):
        return True
    return port in _POOLER_PORT_HINTS


def resolve_uses_pooler(options: dict[str, Any] | None, host: str, port: int) -> bool:
    """True when this target should be treated as sitting behind a transaction/statement-mode
    pooler. `options["uses_pooler"]` (Instance/Node options JSON, set explicitly in the UI)
    always wins when present; otherwise falls back to detect_pooler()'s host/port heuristic."""
    explicit = (options or {}).get("uses_pooler")
    if explicit is not None:
        return bool(explicit)
    return detect_pooler(host, port)


def classify_connection_error(exc: Exception) -> str:
    """Turns a raw driver exception into a Turkish message that names the likely cause —
    wrong password, unreachable host, wrong/closed port, timeout — instead of leaking an
    English driver string as the only signal. The original exception text is kept in
    parentheses so nothing is lost for someone who wants the raw detail."""
    text = str(exc) or exc.__class__.__name__
    lower = text.lower()

    def wrap(prefix: str) -> str:
        return f"{prefix} ({text})"

    if "prepared statement" in lower and ("already exists" in lower or "does not exist" in lower):
        return wrap(
            "Bağlantı bir havuzlayıcı (PgBouncer / Supabase pooler) üzerinden yapılıyor ve "
            "prepared statement hatası alındı. Instance/Node ayarlarında 'Pooler kullanılıyor' "
            "seçeneğini açık olarak işaretleyin."
        )
    if any(
        k in lower
        for k in (
            "password authentication failed",
            "authentication failed",
            "login failed",
            "invalid authorization specification",
            "access denied for user",
            "auth error",
            "unauthorized",
        )
    ):
        return wrap("Kimlik doğrulama başarısız: kullanıcı adı veya parola yanlış.")
    if "permission denied" in lower:
        return wrap(
            "Bağlanan kullanıcının bu işlem için yetkisi yok — sistem kataloğu/view'larını "
            "okuma yetkisi eksik olabilir. GRANT pg_monitor TO <kullanıcı>; (PostgreSQL) veya "
            "GRANT VIEW SERVER STATE TO <login>; (SQL Server) deneyin."
        )
    if any(k in lower for k in ("does not exist", "unknown database", "cannot open database")):
        return wrap("Veritabanı bulunamadı: veritabanı adını kontrol edin.")
    if any(
        k in lower
        for k in (
            "getaddrinfo failed",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
        )
    ):
        return wrap("Sunucuya ulaşılamıyor: host adresi çözümlenemedi (DNS/hostname hatası).")
    if any(k in lower for k in ("connection refused", "actively refused", "connect call failed")):
        return wrap("Bağlantı reddedildi: port kapalı veya yanlış port numarası.")
    if "timed out" in lower or "timeout" in lower:
        return wrap("Bağlantı zaman aşımına uğradı: host erişilebilir mi ve güvenlik duvarı/port açık mı kontrol edin.")
    if any(k in lower for k in ("network is unreachable", "no route to host")):
        return wrap("Ağ erişilemiyor: host'a giden ağ yolu yok.")
    return wrap("Bağlantı başarısız.")


class BaseCollector(ABC):
    @abstractmethod
    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        raise NotImplementedError

    async def open_connection(self) -> Any | None:
        """Opens one connection to share across collect_metrics()/collect_slow_queries() for a
        single collection cycle (see services/collection.py::collect_instance) — avoids opening
        2+ separate connections to the target every collect_interval_seconds. Returns None if
        the collector doesn't support this (each method then opens/closes its own, as before);
        override in collectors whose methods accept an optional `conn` kwarg."""
        return None

    async def close_connection(self, conn: Any | None) -> None:
        if conn is not None:
            await conn.close()

    @abstractmethod
    async def collect_metrics(
        self, previous: dict[str, float] | None = None, conn: Any | None = None
    ) -> dict[str, Any]:
        """Return normalized metrics dict + optional _state for deltas. `conn`: reuse a
        connection from open_connection() when given; collectors that don't support sharing
        (open_connection() returns None) simply ignore it and open their own as before."""

    async def collect_slow_queries(self, limit: int = 20, conn: Any | None = None) -> list[dict[str, Any]]:
        return []

    async def collect_activity(self, limit: int = 100) -> dict[str, Any]:
        """Live session snapshot: sessions, wait events, blocking edges."""
        return {
            "sessions": [],
            "wait_events": [],
            "state_summary": [],
            "blocking": [],
            "totals": {
                "total": 0,
                "active": 0,
                "idle": 0,
                "idle_in_transaction": 0,
                "waiting": 0,
                "blocked": 0,
            },
        }

    async def collect_table_sizes(self, limit: int = 20) -> list[dict[str, Any]]:
        """Top-N tables by size, regardless of bloat status — used for the daily table-growth
        rollup (Faz 16 İŞ 6). Deliberately separate from collect_schema_health's bloated_tables,
        which only lists tables that already qualify as bloated (would silently miss a healthy
        but fast-growing table)."""
        return []

    async def collect_schema_health(self, limit: int = 50) -> dict[str, Any]:
        """Unused indexes + vacuum/bloat signals."""
        return {
            "unused_indexes": [],
            "bloated_tables": [],
            "vacuum_lag": [],
            "totals": {
                "unused_indexes": 0,
                "unused_index_bytes": 0,
                "bloated_tables": 0,
                "vacuum_lag_tables": 0,
            },
        }
