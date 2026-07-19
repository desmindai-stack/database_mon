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


class BaseCollector(ABC):
    @abstractmethod
    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def collect_metrics(self, previous: dict[str, float] | None = None) -> dict[str, Any]:
        """Return normalized metrics dict + optional _state for deltas."""

    async def collect_slow_queries(self, limit: int = 20) -> list[dict[str, Any]]:
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
