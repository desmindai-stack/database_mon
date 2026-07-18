"""Canonical DBA metrics shared across database engines."""

from dataclasses import dataclass

from app.domain.engines import DatabaseEngine


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    display_name: str
    unit: str
    category: str
    engines: frozenset[DatabaseEngine]
    description: str = ""


CANONICAL_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        "active_connections",
        "Active connections",
        "count",
        "connections",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER, DatabaseEngine.MONGODB}),
        "Current client connections",
    ),
    MetricDefinition(
        "max_connections",
        "Max connections",
        "count",
        "connections",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER}),
    ),
    MetricDefinition(
        "connection_utilization_pct",
        "Connection utilization",
        "%",
        "connections",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER, DatabaseEngine.MONGODB}),
    ),
    MetricDefinition(
        "transactions_per_sec",
        "Transactions/sec",
        "tps",
        "performance",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER}),
    ),
    MetricDefinition(
        "cache_hit_ratio",
        "Cache/buffer hit ratio",
        "%",
        "performance",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER, DatabaseEngine.MONGODB}),
    ),
    MetricDefinition(
        "replication_lag_bytes",
        "Replication lag",
        "bytes",
        "replication",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.MONGODB}),
    ),
    MetricDefinition(
        "database_size_bytes",
        "Database size",
        "bytes",
        "storage",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER, DatabaseEngine.MONGODB}),
    ),
    MetricDefinition(
        "deadlocks",
        "Deadlocks",
        "count",
        "reliability",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER}),
    ),
    MetricDefinition(
        "temp_bytes",
        "Temp space used",
        "bytes",
        "storage",
        frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER}),
    ),
    MetricDefinition(
        "ops_per_sec",
        "Operations/sec",
        "ops/s",
        "performance",
        frozenset({DatabaseEngine.MONGODB}),
    ),
)

METRIC_KEYS = {m.key for m in CANONICAL_METRICS}


def metrics_for_engine(engine: DatabaseEngine) -> list[MetricDefinition]:
    return [m for m in CANONICAL_METRICS if engine in m.engines]
