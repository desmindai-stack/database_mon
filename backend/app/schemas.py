from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.engines import DEFAULT_PORTS, DatabaseEngine


class InstanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    engine: DatabaseEngine = DatabaseEngine.POSTGRESQL
    host: str
    port: int | None = None
    database: str = "postgres"
    username: str
    password: str
    options: dict[str, Any] | None = None

    customer_name: str | None = None
    environment: str = "public"
    application: str | None = None
    cluster_name: str | None = None
    role: str | None = None
    services: list[str] | None = None

    def resolved_port(self) -> int:
        if self.port is not None:
            return self.port
        return DEFAULT_PORTS[self.engine]


class InstanceUpdate(BaseModel):
    name: str | None = None
    engine: DatabaseEngine | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    options: dict[str, Any] | None = None
    customer_name: str | None = None
    environment: str | None = None
    application: str | None = None
    cluster_name: str | None = None
    role: str | None = None
    services: list[str] | None = None
    enabled: bool | None = None


class InstanceOut(BaseModel):
    id: int
    name: str
    engine: str
    host: str
    port: int
    database: str
    username: str
    enabled: bool
    created_at: datetime
    customer_name: str | None
    environment: str
    application: str | None
    cluster_name: str | None
    role: str | None
    services: list[str] | None

    model_config = {"from_attributes": True}


class MetricSampleOut(BaseModel):
    id: int
    instance_id: int
    collected_at: datetime
    metrics: dict[str, Any] = Field(default_factory=dict)
    active_connections: int
    max_connections: int
    transactions_per_sec: float
    cache_hit_ratio: float
    replication_lag_bytes: float | None
    database_size_bytes: float
    deadlocks: int
    temp_bytes: float

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_sample(cls, sample) -> "MetricSampleOut":
        metrics = dict(sample.metrics_json or {})
        if "active_connections" not in metrics:
            metrics.update(
                {
                    "active_connections": sample.active_connections,
                    "max_connections": sample.max_connections,
                    "transactions_per_sec": sample.transactions_per_sec,
                    "cache_hit_ratio": sample.cache_hit_ratio,
                    "replication_lag_bytes": sample.replication_lag_bytes,
                    "database_size_bytes": sample.database_size_bytes,
                    "deadlocks": sample.deadlocks,
                    "temp_bytes": sample.temp_bytes,
                }
            )
        return cls(
            id=sample.id,
            instance_id=sample.instance_id,
            collected_at=sample.collected_at,
            metrics=metrics,
            active_connections=sample.active_connections,
            max_connections=sample.max_connections,
            transactions_per_sec=sample.transactions_per_sec,
            cache_hit_ratio=sample.cache_hit_ratio,
            replication_lag_bytes=sample.replication_lag_bytes,
            database_size_bytes=sample.database_size_bytes,
            deadlocks=sample.deadlocks,
            temp_bytes=sample.temp_bytes,
        )


class SlowQueryOut(BaseModel):
    id: int
    instance_id: int
    collected_at: datetime
    queryid: str | None
    query: str
    calls: int
    total_time_ms: float
    mean_time_ms: float
    rows: int

    model_config = {"from_attributes": True}


class AlertRuleCreate(BaseModel):
    instance_id: int | None = None
    name: str
    metric: str
    operator: str
    threshold: float
    enabled: bool = True


class AlertRuleOut(BaseModel):
    id: int
    instance_id: int | None
    name: str
    metric: str
    operator: str
    threshold: float
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertEventOut(BaseModel):
    id: int
    rule_id: int
    instance_id: int
    metric_value: float
    message: str
    triggered_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class PredictionOut(BaseModel):
    id: int
    instance_id: int
    metric_key: str
    created_at: datetime
    horizon_minutes: int
    current_value: float
    predicted_value: float
    threshold: float
    confidence: float
    severity: str
    message: str
    acknowledged_at: datetime | None

    model_config = {"from_attributes": True}


class InstanceSummary(BaseModel):
    instance: InstanceOut
    latest_metrics: MetricSampleOut | None
    status: str
    alerts_firing: int
    predictions_open: int = 0


class HealthResponse(BaseModel):
    status: str
    mode: Literal["api", "worker", "all"]
    instances: int
    last_collection: datetime | None


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = {}


class MetricDefinitionOut(BaseModel):
    key: str
    display_name: str
    unit: str
    category: str
    engines: list[str]
    description: str
