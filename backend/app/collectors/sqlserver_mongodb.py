from __future__ import annotations

import logging
from typing import Any

from app.collectors.base import BaseCollector, ConnectionTarget

logger = logging.getLogger(__name__)


class SqlServerCollector(BaseCollector):
    """SQL Server collector — implement with pyodbc/aioodbc in production worker."""

    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            import aioodbc  # noqa: F401
        except ImportError:
            return (
                False,
                "SQL Server collector requires aioodbc on the worker image (henüz tam bağlantı yok).",
                {"engine": "sqlserver", "status": "stub"},
            )
        return False, "SQL Server collector implementation in progress", {"engine": "sqlserver"}

    async def collect_metrics(self, previous: dict[str, float] | None = None) -> dict[str, Any]:
        raise NotImplementedError("SQL Server metrics collection not yet implemented")


class MongoDBCollector(BaseCollector):
    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError:
            return (
                False,
                "MongoDB collector requires motor on the worker image.",
                {"engine": "mongodb", "status": "stub"},
            )

        uri = self._build_uri()
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        try:
            info = await client.server_info()
            return True, "Connection successful", {
                "engine": "mongodb",
                "version": info.get("version"),
            }
        except Exception as exc:
            logger.exception("MongoDB connection test failed")
            return False, str(exc), {}
        finally:
            client.close()

    def _build_uri(self) -> str:
        auth = f"{self.target.username}:{self.target.password}@"
        db = self.target.database or "admin"
        opts = self.target.options or {}
        auth_source = opts.get("authSource", db)
        return (
            f"mongodb://{auth}{self.target.host}:{self.target.port}/{db}"
            f"?authSource={auth_source}"
        )

    async def collect_metrics(self, previous: dict[str, float] | None = None) -> dict[str, Any]:
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(self._build_uri(), serverSelectionTimeoutMS=8000)
        try:
            status = await client.admin.command("serverStatus")
            conn = status.get("connections", {})
            active = int(conn.get("current", 0))
            available = int(conn.get("available", 0))
            max_conn = active + available
            util = (active / max_conn * 100) if max_conn else 0.0

            opcounters = status.get("opcounters", {})
            total_ops = sum(float(opcounters.get(k, 0)) for k in ("insert", "query", "update", "delete"))
            ops_per_sec = 0.0
            if previous and "total_ops" in previous:
                delta = total_ops - previous["total_ops"]
                delta_time = previous.get("delta_time", 15)
                ops_per_sec = max(delta / delta_time, 0)

            wired = status.get("wiredTiger", {}).get("cache", {})
            bytes_in = float(wired.get("bytes read into cache", 0))
            bytes_out = float(wired.get("bytes written from cache", 0))
            hit_ratio = 100.0
            if bytes_in + bytes_out > 0:
                hit_ratio = bytes_out / (bytes_in + bytes_out) * 100

            db_stats = await client[self.target.database or "admin"].command("dbStats")
            size = float(db_stats.get("dataSize", 0))

            repl = status.get("repl", {})
            lag_bytes = None
            if repl.get("ismaster") is False:
                lag_bytes = float(repl.get("lag", 0) or 0)

            return {
                "active_connections": active,
                "max_connections": max_conn,
                "connection_utilization_pct": round(util, 2),
                "ops_per_sec": round(ops_per_sec, 2),
                "cache_hit_ratio": round(hit_ratio, 2),
                "replication_lag_bytes": lag_bytes,
                "database_size_bytes": size,
                "deadlocks": 0,
                "temp_bytes": 0.0,
                "_state": {"total_ops": total_ops},
            }
        finally:
            client.close()
