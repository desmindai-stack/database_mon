from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InstanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str
    port: int = 5432
    database: str = "postgres"
    username: str
    password: str


class InstanceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    enabled: bool | None = None


class InstanceOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    database: str
    username: str
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class MetricSampleOut(BaseModel):
    id: int
    instance_id: int
    collected_at: datetime
    active_connections: int
    max_connections: int
    transactions_per_sec: float
    cache_hit_ratio: float
    replication_lag_bytes: float | None
    database_size_bytes: float
    deadlocks: int
    temp_bytes: float

    model_config = {"from_attributes": True}


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


class InstanceSummary(BaseModel):
    instance: InstanceOut
    latest_metrics: MetricSampleOut | None
    status: str
    alerts_firing: int


class HealthResponse(BaseModel):
    status: str
    instances: int
    last_collection: datetime | None


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = {}
