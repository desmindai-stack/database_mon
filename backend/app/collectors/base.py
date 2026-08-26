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


def classify_connection_error(exc: Exception) -> str:
    """Turns a raw driver exception into a Turkish message that names the likely cause —
    wrong password, unreachable host, wrong/closed port, timeout — instead of leaking an
    English driver string as the only signal. The original exception text is kept in
    parentheses so nothing is lost for someone who wants the raw detail."""
    text = str(exc) or exc.__class__.__name__
    lower = text.lower()

    def wrap(prefix: str) -> str:
        return f"{prefix} ({text})"

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
