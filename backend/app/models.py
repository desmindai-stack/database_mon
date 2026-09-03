from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="viewer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Forced on the initial admin bootstrap (and after an admin resets a user's password) —
    # cleared once the user successfully calls POST /api/auth/change-password.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    access_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cluster_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vip_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # SQL Server Always On listener port / PostgreSQL HAProxy-VIP port — independent of any
    # individual node's instance port (a listener commonly uses a different port than the
    # instances behind it, e.g. HAProxy on 5000 fronting Postgres on 5432).
    listener_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    application: Mapped["Application"] = relationship(back_populates="groups")
    nodes: Mapped[list["Node"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    instances: Mapped[list["Instance"]] = relationship(back_populates="group")


class Server(Base):
    """A physical/VM host. Linux+PostgreSQL: one service per server, so effectively 1
    server = 1 Node. Windows+SQL Server: a box can run several named instances (Nodes),
    each potentially in a different Always On group — hence Server and Node are split.
    Service/OS/log access all come from the Server (one host-agent per machine, serving
    every instance on it)."""

    __tablename__ = "servers"
    __table_args__ = (UniqueConstraint("customer_id", "name", name="uq_server_customer_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    # In addition to the hostname — useful when DNS isn't reliable/set up yet, or to record the
    # address separately from whatever name is used to reach it.
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os: Mapped[str] = mapped_column(String(16), default="linux", nullable=False)
    site: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)
    agent_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    customer: Mapped["Customer"] = relationship()
    nodes: Mapped[list["Node"]] = relationship(back_populates="server")


class Node(Base):
    """One database instance/service — on Linux this is effectively "the server", on
    Windows it's one named SQL Server instance among possibly several on the same Server.
    Group (cluster) membership lives here, at the instance level, so two instances on the
    same physical server can belong to two different Always On groups."""

    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_node_group_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id"), nullable=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("database_groups.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # SQL Server named instance (e.g. "MSSQLSERVER" for default, or a custom name) — not
    # meaningful for PostgreSQL, where a server only ever runs one instance.
    instance_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    role_hint: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped["DatabaseGroup"] = relationship(back_populates="nodes")
    instance: Mapped["Instance | None"] = relationship(foreign_keys=[instance_id])
    server: Mapped["Server | None"] = relationship(back_populates="nodes")


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

    # Collector-derived, read-only — refreshed on every successful collect_metrics() run
    # (services/collection.py). server_version is a human-readable label (e.g. "PostgreSQL
    # 17.0 ..." / "SQL Server 2022 (16.0...)"); unsupported_metrics maps a metric key to a
    # short Turkish reason when this server's version doesn't support it (see
    # collectors/postgresql.py, collectors/sqlserver_mongodb.py).
    server_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unsupported_metrics: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)

    customer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment: Mapped[str] = mapped_column(String(32), default="public", nullable=False)
    application: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cluster_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    services: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    group_id: Mapped[int | None] = mapped_column(ForeignKey("database_groups.id"), nullable=True)

    # Null = use the app-wide settings.collect_interval_seconds default. Lets a lower-priority
    # instance be sampled less often — see services/collection.py / collectors/scheduler.py.
    collect_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    # "metric": evaluated against already-collected metrics_json (existing behavior).
    # "custom": evaluated by running rule.sql_query live against the target (Faz 8 İŞ 5).
    rule_type: Mapped[str] = mapped_column(String(16), default="metric", nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    operator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="warning", nullable=False)
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sql_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class AppSetting(Base):
    """Small global key/value store for operational settings (e.g. dashboard refresh
    interval) — there is no per-user auth model in dbace, so settings are shared/global."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


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
    # Suggested next step (Faz 15 İŞ 6) — prose, deliberately non-numeric where a specific
    # setting is involved so it can't contradict parameter_audit's own value-aware finding for
    # that same setting. action is an optional copy-pasteable command, same pattern as the
    # dashboard's recommendation cards.
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # %90 tahmin aralığı + kullanılan mevsimsellik modeli (Faz 16 İŞ 6) — regresyonun kalıntı
    # varyansından türetilen gerçek bir istatistiksel aralık, göstermelik bir sayı değil.
    lower_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    seasonality: Mapped[str | None] = mapped_column(String(16), nullable=True)

    instance: Mapped["Instance"] = relationship(back_populates="predictions")


class MetricRollupDaily(Base):
    """Faz 16 İŞ 6: günlük özet — ham `MetricSample` satırları 1 aylık saklama süresinden sonra
    silinir (services/retention.py), ama uzun vadeli tahminler (disk dolma tarihi, wraparound)
    haftalar/aylar süren bir trend ister. Bu tablo her instance/metrik/gün için TEK bir satır
    tutar ve retention temizliğinden MUAFTIR (ham örneklerden çok daha küçük hacimli — bkz.
    SORULAR.md)."""

    __tablename__ = "metric_rollup_daily"
    __table_args__ = (UniqueConstraint("instance_id", "metric_key", "day", name="uq_metric_rollup_instance_key_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True, nullable=False)
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    avg_value: Mapped[float] = mapped_column(Float, nullable=False)
    min_value: Mapped[float] = mapped_column(Float, nullable=False)
    max_value: Mapped[float] = mapped_column(Float, nullable=False)
    last_value: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaObjectDailySample(Base):
    """Faz 16 İŞ 6: tablo/index boyutunun günlük anlık görüntüsü — `collect_schema_health()`'in
    (zaten var olan, on-demand kullanılan) tek bir katalog taramasından günde bir kez türetilir.
    "Tablo büyüme hızı" ve "index şişmesi" tahminleri bu tabloya dayanır."""

    __tablename__ = "schema_object_daily_samples"
    __table_args__ = (
        UniqueConstraint(
            "instance_id", "object_kind", "schema_name", "object_name", "day",
            name="uq_schema_object_daily",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "table" | "index"
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    object_name: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[float] = mapped_column(Float, nullable=False)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
