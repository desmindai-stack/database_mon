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

    shared_blks_hit: int | None
    shared_blks_read: int | None
    local_blks_hit: int | None
    local_blks_read: int | None
    temp_blks_read: int | None
    temp_blks_written: int | None

    plan_user_time: float | None
    plan_sys_time: float | None
    exec_user_time: float | None
    exec_sys_time: float | None

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
    deployment_mode: str
    default_customer_name: str | None
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


class IndexAdviceRequest(BaseModel):
    query: str = Field(min_length=1)


class IndexAdviceOut(BaseModel):
    table_name: str
    schema_name: str
    columns: list[str]
    index_ddl: str
    reason: str
    estimated_improvement_pct: float
    has_hypopg_estimate: bool
    before_cost: float | None
    after_cost: float | None
    existing_indexes: list[str]


class PerformanceInsightOut(BaseModel):
    severity: str
    category: str
    title: str
    description: str
    recommendation: str
    metric_value: float | None
    metric_unit: str | None
    action: str | None = None


class TuningChecklistOut(BaseModel):
    key: str
    label: str
    status: str
    detail: str


class TuningReportOut(BaseModel):
    health_score: int
    grade: str
    status: str
    collected_at: datetime | None
    summary: dict[str, int]
    insights: list[PerformanceInsightOut]
    checklist: list[TuningChecklistOut]


class ActivitySessionOut(BaseModel):
    pid: int
    usename: str | None
    datname: str | None
    application_name: str
    client_addr: str | None
    state: str
    wait_event_type: str | None
    wait_event: str | None
    backend_type: str | None
    query_start: str | None
    state_change: str | None
    xact_start: str | None
    query_duration_sec: float
    xact_duration_sec: float | None
    query: str
    blocking_pids: list[int]
    blocked: bool


class WaitEventOut(BaseModel):
    wait_event_type: str
    wait_event: str
    count: int


class StateCountOut(BaseModel):
    state: str
    count: int


class BlockingEdgeOut(BaseModel):
    blocked_pid: int
    blocking_pid: int
    blocked_query: str
    wait_event_type: str | None
    wait_event: str | None
    duration_sec: float


class ActivityTotalsOut(BaseModel):
    total: int
    active: int
    idle: int
    idle_in_transaction: int
    waiting: int
    blocked: int


class ActivityOut(BaseModel):
    sessions: list[ActivitySessionOut]
    wait_events: list[WaitEventOut]
    state_summary: list[StateCountOut]
    blocking: list[BlockingEdgeOut]
    totals: ActivityTotalsOut


class ExplainRequest(BaseModel):
    query: str = Field(min_length=1)
    analyze: bool = False


class ExplainPlanNodeOut(BaseModel):
    node_type: str
    relation_name: str | None = None
    alias: str | None = None
    startup_cost: float | None = None
    total_cost: float | None = None
    plan_rows: float | None = None
    plan_width: float | None = None
    actual_total_time: float | None = None
    actual_rows: float | None = None
    shared_hit_blocks: float | None = None
    shared_read_blocks: float | None = None
    insights: list[str] = []
    children: list["ExplainPlanNodeOut"] = []


class ExplainOut(BaseModel):
    query: str
    analyzed: bool
    planning_time_ms: float | None
    execution_time_ms: float | None
    total_cost: float | None
    insights: list[str]
    plan: ExplainPlanNodeOut | None
    raw_plan: list[Any] = []


ExplainPlanNodeOut.model_rebuild()


class QueryHistoryPointOut(BaseModel):
    collected_at: datetime
    calls: int
    total_time_ms: float
    mean_time_ms: float
    rows: int
    calls_delta: int | None = None
    total_time_delta_ms: float | None = None
    interval_mean_ms: float | None = None


class QueryHistorySeriesOut(BaseModel):
    queryid: str
    query: str
    points: list[QueryHistoryPointOut]
    latest_mean_ms: float
    latest_calls: int
    max_mean_ms: float
    min_mean_ms: float
    avg_mean_ms: float
    calls_delta_sum: int
    trend_pct: float


class QueryHistoryListOut(BaseModel):
    hours: int
    series: list[QueryHistorySeriesOut]


class UnusedIndexOut(BaseModel):
    schema_name: str
    table_name: str
    index_name: str
    index_bytes: int
    idx_scan: int
    idx_tup_read: int
    idx_tup_fetch: int
    index_def: str
    drop_ddl: str


class BloatedTableOut(BaseModel):
    schema_name: str
    table_name: str
    live_tup: int
    dead_tup: int
    dead_ratio_pct: float
    table_bytes: int
    last_vacuum: str | None
    last_autovacuum: str | None
    last_analyze: str | None
    last_autoanalyze: str | None
    freeze_age: int
    severity: str


class VacuumLagOut(BaseModel):
    schema_name: str
    table_name: str
    live_tup: int
    dead_tup: int
    last_autovacuum: str | None
    last_autoanalyze: str | None
    lag_sec: float
    freeze_age: int
    severity: str


class SchemaHealthTotalsOut(BaseModel):
    unused_indexes: int
    unused_index_bytes: int
    bloated_tables: int
    vacuum_lag_tables: int


class SchemaHealthOut(BaseModel):
    unused_indexes: list[UnusedIndexOut]
    bloated_tables: list[BloatedTableOut]
    vacuum_lag: list[VacuumLagOut]
    totals: SchemaHealthTotalsOut
