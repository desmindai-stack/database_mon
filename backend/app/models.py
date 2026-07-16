from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=5432)
    database: Mapped[str] = mapped_column(String(128), default="postgres")
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    metrics: Mapped[list["MetricSample"]] = relationship(back_populates="instance")
    slow_queries: Mapped[list["SlowQuerySample"]] = relationship(back_populates="instance")
    alert_rules: Mapped[list["AlertRule"]] = relationship(back_populates="instance")


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, index=True, server_default=func.now())

    active_connections: Mapped[int] = mapped_column(Integer, default=0)
    max_connections: Mapped[int] = mapped_column(Integer, default=0)
    transactions_per_sec: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    replication_lag_bytes: Mapped[float | None] = mapped_column(Float, nullable=True)
    database_size_bytes: Mapped[float] = mapped_column(Float, default=0.0)
    deadlocks: Mapped[int] = mapped_column(Integer, default=0)
    temp_bytes: Mapped[float] = mapped_column(Float, default=0.0)

    instance: Mapped["Instance"] = relationship(back_populates="metrics")


class SlowQuerySample(Base):
    __tablename__ = "slow_query_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, index=True, server_default=func.now())

    queryid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    total_time_ms: Mapped[float] = mapped_column(Float, default=0.0)
    mean_time_ms: Mapped[float] = mapped_column(Float, default=0.0)
    rows: Mapped[int] = mapped_column(Integer, default=0)

    instance: Mapped["Instance"] = relationship(back_populates="slow_queries")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    instance: Mapped["Instance | None"] = relationship(back_populates="alert_rules")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
