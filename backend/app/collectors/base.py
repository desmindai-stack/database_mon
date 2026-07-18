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
