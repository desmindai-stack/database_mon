"""Fake async DB connection for collector unit tests — no real network I/O.

Responses are matched by substring against the SQL text, in insertion order (first match
wins), so tests only need to name the distinctive part of a query (e.g. "pg_stat_checkpointer")
rather than reproduce it verbatim. An unmatched query raises AssertionError instead of
silently returning None, so a test that forgets to stub a query fails loudly instead of
masking a real bug as "this metric came back empty".
"""

from __future__ import annotations

from typing import Any, Callable


class FakeAsyncConnection:
    def __init__(self, responses: dict[str, Any | Callable[..., Any]]) -> None:
        self._responses = responses
        self.queries: list[str] = []

    def _match(self, sql: str) -> Any:
        self.queries.append(sql)
        for needle, value in self._responses.items():
            if needle in sql:
                return value(sql) if callable(value) else value
        raise AssertionError(f"FakeAsyncConnection: no response configured for query:\n{sql[:300]}")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._match(sql)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = self._match(sql)
        if isinstance(row, dict) and len(row) == 1:
            return next(iter(row.values()))
        return row

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        row = self._match(sql)
        return row if row is not None else []

    async def execute(self, sql: str, *args: Any) -> str:
        # Fire-and-forget statements (SET ...) — no configured response needed, always
        # "succeeds" (matching asyncpg.Connection.execute's plain command-tag return).
        self.queries.append(sql)
        return "SET"

    async def close(self) -> None:
        return None


class FakeCursor:
    """Fake aioodbc-style cursor (execute/fetchone/fetchall/description, async context manager)."""

    def __init__(self, conn: "FakeSqlServerConnection") -> None:
        self._conn = conn
        self._result: Any = None
        self.description: list[tuple[str, ...]] = []

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        self._result, self.description = self._conn._match(sql)

    async def fetchone(self) -> Any:
        if not self._result:
            return None
        return self._result[0]

    async def fetchall(self) -> list[Any]:
        return self._result or []


class FakeSqlServerConnection:
    def __init__(self, responses: dict[str, Any | Callable[..., Any]]) -> None:
        self._responses = responses
        self.queries: list[str] = []

    def _match(self, sql: str) -> tuple[list[Any], list[tuple[str, ...]]]:
        self.queries.append(sql)
        for needle, value in self._responses.items():
            if needle in sql:
                resolved = value(sql) if callable(value) else value
                return resolved
        raise AssertionError(f"FakeSqlServerConnection: no response configured for query:\n{sql[:300]}")

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    async def close(self) -> None:
        return None
