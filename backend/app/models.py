from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    applications: Mapped[list["Application"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("customer_id", "name", name="uq_application_customer_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    customer: Mapped["Customer"] = relationship(back_populates="applications")
    groups: Mapped[list["DatabaseGroup"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class DatabaseGroup(Base):
    __tablename__ = "database_groups"
    __table_args__ = (UniqueConstraint("application_id", "name", name="uq_group_application_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), default="postgresql", nullable=False)
    topology: Mapped[str] = mapped_column(String(32), default="standalone", nullable=False)
    environment: Mapped[str] = mapped_column(String(16), default="prod", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    application: Mapped["Application"] = relationship(back_populates="groups")
    nodes: Mapped[list["Node"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    instances: Mapped[list["Instance"]] = relationship(back_populates="group")


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_node_group_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("database_groups.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    site: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)
    role_hint: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    agent_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped["DatabaseGroup"] = relationship(back_populates="nodes")


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    engine: Mapped[str] = mapped_column(String(32), default="postgresql", nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=5432)
    database: Mapped[str] = mapped_column(String(128), default="postgres")
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password: Mapped[str] = mapped_column(String(512), nullable=False)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    customer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment: Mapped[str] = mapped_column(String(32), default="public", nullable=False)
    application: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cluster_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    services: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    group_id: Mapped[int | None] = mapped_column(ForeignKey("database_groups.id"), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    metrics: Mapped[list["MetricSample"]] = relationship(back_populates="instance")
    slow_queries: Mapped[list["SlowQuerySample"]] = relationship(back_populates="instance")
    alert_rules: Mapped[list["AlertRule"]] = relationship(back_populates="instance")
    predictions: Mapped[list["PredictionInsight"]] = relationship(back_populates="instance")
    group: Mapped["DatabaseGroup | None"] = relationship(back_populates="instances")


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, server_default=func.now())
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    active_connections: Mapped[int] = mapped_column(Integer, default=0)
    max_connections: Mapped[int] = mapped_column(Integer, default=0)
    transactions_per_sec: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    replication_lag_bytes: Mapped[float | None] = mapped_column(Float, nullable=True)
    database_size_bytes: Mapped[float] = mapped_column(Float, default=0.0)
    deadlocks: Mapped[int] = mapped_column(Integer, default=0)
    temp_bytes: Mapped[float] = mapped_column(Float, default=0.0)

    instance: Mapped["Instance"] = relationship(back_populates="metrics")

    def get_metric(self, key: str) -> float | int | None:
        if self.metrics_json and key in self.metrics_json:
            val = self.metrics_json[key]
            return None if val is None else val
        legacy = {
            "active_connections": self.active_connections,
            "max_connections": self.max_connections,
            "transactions_per_sec": self.transactions_per_sec,
            "cache_hit_ratio": self.cache_hit_ratio,
            "replication_lag_bytes": self.replication_lag_bytes,
            "database_size_bytes": self.database_size_bytes,
            "deadlocks": self.deadlocks,
            "temp_bytes": self.temp_bytes,
        }
        return legacy.get(key)


class SlowQuerySample(Base):
    __tablename__ = "slow_query_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, server_default=func.now())

    queryid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    total_time_ms: Mapped[float] = mapped_column(Float, default=0.0)
    mean_time_ms: Mapped[float] = mapped_column(Float, default=0.0)
    rows: Mapped[int] = mapped_column(Integer, default=0)

    shared_blks_hit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shared_blks_read: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_blks_hit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_blks_read: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_blks_read: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_blks_written: Mapped[int | None] = mapped_column(Integer, nullable=True)

    plan_user_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    plan_sys_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_user_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_sys_time: Mapped[float | None] = mapped_column(Float, nullable=True)

    instance: Mapped["Instance"] = relationship(back_populates="slow_queries")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("database_groups.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    instance: Mapped["Instance | None"] = relationship(back_populates="alert_rules")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id"), index=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), index=True, nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("database_groups.id"), index=True, nullable=True)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GroupHealthSnapshot(Base):
    __tablename__ = "group_health_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("database_groups.id"), unique=True, index=True, nullable=False)
    overall: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    recommendations_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PredictionInsight(Base):
    __tablename__ = "prediction_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    horizon_minutes: Mapped[int] = mapped_column(Integer, default=60)
    current_value: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_value: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped["Instance"] = relationship(back_populates="predictions")
