from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
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

    # Faz 16-B İŞ 6: kullanıcının "bu kontrolü yoksay" dediği ön koşul anahtarları
    # (services/prerequisites.py'deki PrerequisiteCheck.key değerleri). Yoksayılan kontroller
    # ön koşul yüzdesine ve dashboard önerilerine dahil edilmez; ortamda kullanılmayacak bir
    # uzantı yüzünden liste sonsuza kadar kırmızı kalmasın diye. Null/boş = hiçbiri yoksayılmadı.
    ignored_prerequisites: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

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
    # "Bu instance'ın EN SON örneği" bu tablonun en sık sorusu (insights, dashboard, DPA,
    # tahminler). Ayrı ayrı `instance_id` ve `collected_at` indeksleri bu soruyu ucuza
    # cevaplayamıyordu: ya instance'ın tüm satırları çekilip sıralanıyor, ya da `collected_at`
    # indeksi sondan taranıp instance_id ile eleniyordu. İkincisi, veri göndermeyi durdurmuş
    # bir instance için tablonun tamamını taramaya dönüşüyor — canlıda 502'nin sebebi buydu.
    __table_args__ = (Index("ix_metric_samples_instance_collected", "instance_id", "collected_at"),)

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
    # Aynı erişim kalıbı, daha da kritik: bu tabloya toplama döngüsü başına 20 satır yazılıyor
    # (15 sn aralıkla instance başına ~3.5 milyon satır/ay), yani sıralama maliyeti
    # metric_samples'takinin 20 katı.
    __table_args__ = (
        Index("ix_slow_query_samples_instance_collected", "instance_id", "collected_at"),
    )

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
    # Faz 16-B İŞ 7: adım adım çözüm planı — [{title, detail, command}]. `recommendation` tek
    # cümlelik özet olarak kalıyor; bu, o özetin "önce şunu çalıştır, çıktısına göre şunu yap"
    # açılımı. Kaydedildiği anda üretiliyor ki tahmin geçmişi kendi planını taşısın.
    playbook: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Faz 20 İŞ 1: beş parçalı standart öneri (neden / adımlar / komutlar / dikkat / doğrulama).
    # `PredictionOut.advice` Faz 17'de şemaya eklenmişti ama modelde karşılığı YOKTU — API her
    # tahmin için `advice: null` dönüyor, arayüz tek cümlelik `recommendation`'a düşüyordu.
    # `playbook` ham adım listesi olarak duruyor (geriye dönük uyumluluk); bu onun standart hali.
    advice: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Faz 20 İŞ 3 — yöntem şeffaflığı: hangi model, kaç ölçüm, hangi dönem, kaç aykırı değer
    # atıldı ve veri doğrusal modele uyuyor mu. Kullanıcı tahminin neye dayandığını görebilsin
    # (kara kutu olmasın); `fit_kind` "linear" değilse arayüz uyarı gösteriyor.
    method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    outliers_removed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fit_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fit_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Eşiğe/hedefe ulaşma tarihinin ARALIĞI — tek nokta yerine "45-60 gün arası".
    eta_days_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta_days_max: Mapped[float | None] = mapped_column(Float, nullable=True)

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


class PredictionOutcome(Base):
    """Tahmin doğruluğunun ÖLÇÜLDÜĞÜ tablo (Faz 20 İŞ 2).

    Öncesinde dbace her tahmine bir `confidence` yazıyordu ama bu yalnızca regresyonun
    R² değeriydi — "model geçmiş veriye ne kadar iyi oturdu" demek, "tahmin tuttu mu"
    demek DEĞİL. Doğruluk iddia edilemez, ölçülür: her tahmin üretildiğinde buraya bir
    satır yazılır, hedef tarih geldiğinde gerçekleşen değer okunur ve sapma hesaplanır.

    **`predicted_value` neden `PredictionInsight.predicted_value` ile aynı olmayabilir:**
    uzun vadeli tahminlerin manşet ufku aylar sürer (disk için 180 gün) — o tarihi beklemek
    altı ay boyunca hiçbir geri besleme almamak demekti. Bunun yerine AYNI modelden
    `checkpoint_days` gün sonrası için ikinci bir tahmin alınıp burada saklanıyor. Ölçülen
    şey modelin kendisidir; kısa bir kontrol noktası bunu aylar beklemeden ölçer. Kısa
    vadeli tahminlerde kontrol noktası zaten manşet ufkun kendisidir.
    """

    __tablename__ = "prediction_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prediction_id: Mapped[int | None] = mapped_column(
        ForeignKey("prediction_insights.id", ondelete="SET NULL"), index=True, nullable=True
    )
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True, nullable=False)
    # Tahmin AİLESİ — doğruluk metrikleri bu kırılımda tutuluyor (metric_key değil: tablo/index
    # tahminlerinde metric_key nesne adını taşır ve her nesne ayrı bir kova olurdu).
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    checkpoint_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    predicted_value: Mapped[float] = mapped_column(Float, nullable=False)
    lower_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_bound: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Kullanılan yöntem ve dayandığı veri — "kara kutu olmasın" (İŞ 3).
    method: Mapped[str] = mapped_column(String(64), nullable=False, default="linear_regression")
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    span_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    r_squared: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Gerçekleşen değerin nereden okunacağı: "sample" (MetricSample), "rollup"
    # (MetricRollupDaily), "schema_object" (SchemaObjectDailySample).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Değerlendirme sonucu. status: "pending" | "evaluated" | "expired".
    # "expired" = hedef tarih geçti ama gerçekleşen değer okunamadı (instance kapatılmış,
    # toplama durmuş, nesne silinmiş) — bunu "hatalı tahmin" saymak yanlış olurdu.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    within_interval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unevaluable_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


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


class HealthReport(Base):
    """Faz 17 İŞ 1: bir kapsam için üretilmiş sağlık raporu.

    Rapor CANLI PROBE YAPMAZ — tamamen toplanmış veriden (MetricSample, SlowQuerySample,
    AlertEvent, PredictionInsight, GroupHealthSnapshot, rollup tabloları) türetilir. Sebep:
    rapor 06:00'da onlarca instance için çalışır; her biri için canlı bağlantı açmak hem
    toplama döngüsüyle yarışır hem de raporun "dün ne oldu" sorusuna cevap vermesi gerekirken
    "şu an ne oluyor" sorusuna cevap vermesine yol açardı.

    `sections` bölüm gövdelerini (özet metinleri, tablolar, grafik verisi) tutar; tek tek
    bulgular ayrı satırlar olarak ReportFinding'de durur — böylece fingerprint üzerinden
    günler arası karşılaştırma ve kabul (acknowledgement) mümkün olur.
    """

    __tablename__ = "health_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # scope_type: global | customer | application | group | instance. "global" için scope_id NULL.
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Kapsamın o anki adı — kapsam sonradan silinse/yeniden adlandırılsa bile rapor kendi
    # başlığını taşısın diye kopyalanıyor (rapor geçmişi bir arşivdir, canlı bir görünüm değil).
    scope_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    generated_by: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)  # schedule | manual
    # ok | info | warning | critical — bölümlerin en kötüsü.
    overall_status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    sections: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Aynı kapsamın bir önceki raporu — "dünden beri değişenler" bölümü bunun bulgularıyla
    # karşılaştırarak üretilir.
    previous_report_id: Mapped[int | None] = mapped_column(ForeignKey("health_reports.id"), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Arka planda üretim durumu: queued | running | done | failed. Rapor satırı üretim
    # başlarken oluşturuluyor ki arayüz ilerlemeyi gösterebilsin.
    status: Mapped[str] = mapped_column(String(16), default="done", nullable=False, index=True)
    progress_pct: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    progress_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    findings: Mapped[list["ReportFinding"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class ReportFinding(Base):
    """Rapordaki tek bir bulgu.

    `fingerprint` aynı bulgunun günler arasında eşleşmesini sağlayan kararlı bir hash'tir
    (bulgu tipi + hedef nesne; ölçülen DEĞER hash'e girmez, yoksa değer her değiştiğinde bulgu
    "yeni" görünür ve "kaç gündür açık" sayısı hiç ilerlemezdi).
    """

    __tablename__ = "report_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("health_reports.id"), index=True, nullable=False)
    section: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # critical | warning | info | ok
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Bulgunun DAYANDIĞI veri: {metric, value, threshold, measured_at, ...}. Kanıtsız bulgu
    # üretilmiyor (Faz 17 İŞ 6) — bu alan boş bırakılamaz.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    commands: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    related_object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Faz 18 İŞ 1: bulgunun işaret ettiği kesin hedef URL'i (sorgu anahtarı + pencere dahil).
    # Boşsa arayüz bölüm→sekme eşlemesine düşer.
    link_hint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Faz 18 İŞ 3: kısa sınırlılık notu (ör. "darboğaz belirlenemedi: CPU verisi yok").
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Faz 18 İŞ 4: sayısal özet — [{label, value, tone}]. Uzun paragraf yerine etiketli satırlar.
    facts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "<bölüm>:<bulgu tipi>" — hedef nesne kimliği içermez. Grup/uygulama/müşteri/küresel
    # kapsamlı durum kararlarının aynı TİPTEKİ bulguları eşleştirebilmesi için (Ek İŞ A).
    finding_type: Mapped[str] = mapped_column(String(96), nullable=False, default="", index=True)
    # Bu rapordaki etkin durum: open | ignored | deferred | risk_accepted | planned |
    # resolved_pending_verification | resolved.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    # "çözüldü_doğrulanacak" denmişti ama bulgu hâlâ tespit ediliyor — yanlış kapatma işareti.
    verification_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Kararın kendisi (not/referans/süre) bulguyla birlikte gösterilebilsin diye kopyalanıyor.
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Faz 17 Ek İŞ B: standart öneri yapısı (başlık/neden/adımlar/komutlar/dikkat/doğrulama).
    # `recommendation` + `commands` alanları geriye dönük uyumluluk için duruyor; bu alan
    # onların yapılandırılmış hali ve arayüzde gösterilen asıl kaynak.
    advice: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Etki × aciliyet sıralaması için hesaplanan puan (Faz 17 İŞ 6) — büyük olan üstte.
    priority: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Bu fingerprint kaç gündür kesintisiz açık (önceki raporlardan devralınır).
    open_since_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # new | ongoing | resolved | regressed — "dünden beri değişenler" bölümünün ham verisi.
    change_state: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    report: Mapped["HealthReport"] = relationship(back_populates="findings")


class FindingAcknowledgement(Base):
    """Bir bulgu için verilmiş DURUM KARARI (Faz 17 Ek İŞ A).

    Başlangıçta yalnızca "kabul edildi" bilgisini tutuyordu; artık bir durum makinesinin
    kaydı: açık / yoksayıldı / ertelendi / risk_kabul / planlandı / çözüldü_doğrulanacak /
    çözüldü. Tablo adı `finding_acknowledgements` olarak KORUNDU — yeniden adlandırmak var
    olan kurulumlarda veri taşıma gerektirirdi; kolon eklemek yeterli (repo'daki yerleşik
    migration deseni).

    **Kapsam (scope) artık kararın GENİŞLİĞİ.** Kullanıcı kararı şu seviyelerden birine
    uygular:

    * `instance` (varsayılan, en dar) — yalnızca bu tekil bulgu. `fingerprint` ile eşleşir;
      fingerprint hedef nesneyi zaten içerdiği için başka bir sunucudaki aynı tip bulgu
      etkilenmez.
    * `group` / `application` / `customer` — bu bulgu TİPİ, o birime bağlı tüm
      instance'larda. `finding_type` ile eşleşir.
    * `global` — bu bulgu tipi her yerde.

    En dar kapsamın varsayılan olması bilinçli: bir sunucuda verilen "yoksay" kararının
    sessizce tüm filoyu susturması, raporun amacına aykırı olurdu.
    """

    __tablename__ = "finding_acknowledgements"
    __table_args__ = (
        UniqueConstraint("fingerprint", "scope_type", "scope_id", name="uq_ack_fingerprint_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # instance kapsamında tekil bulgunun kimliği; daha geniş kapsamlarda bu alan kararın
    # verildiği ilk bulgunun fingerprint'ini taşır (izlenebilirlik için) ama eşleşme
    # finding_type üzerinden yapılır.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "<bölüm>:<bulgu tipi>" — hedef nesne kimliği İÇERMEZ, bu yüzden birim/küresel kapsamda
    # aynı tip bulguları eşleştirebilir.
    finding_type: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="instance")
    # NULL = global (bu bulgu tipi her yerde).
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ignored")
    acknowledged_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # "ertelendi" için erteleme bitiş tarihi; "yoksayıldı" için opsiyonel süre. Geçtiğinde
    # bulgu kendiliğinden "açık"a döner.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "planlandı" için serbest metin referans: değişiklik talebi no, ticket no, planlanan tarih.
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Not her durum değişikliğinde ZORUNLU (router'da doğrulanıyor) — "neden bu karar verildi"
    # sorusunun cevabı olmadan bir susturma kaydı altı ay sonra anlamsız hale gelir.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FindingStatusHistory(Base):
    """Durum değişikliği geçmişi (Faz 17 Ek İŞ A).

    Her geçiş ayrı bir satır: kim, ne zaman, hangi durumdan hangisine, hangi notla. Otomatik
    geçişler (erteleme süresi doldu, çözüm doğrulanamadı, bulgu kayboldu) de buraya yazılır ve
    `changed_by="sistem"` ile işaretlenir — böylece "bunu kim açtı?" sorusu her zaman
    cevaplanabilir.
    """

    __tablename__ = "finding_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    finding_type: Mapped[str | None] = mapped_column(String(96), nullable=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="instance")
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DailyStateSnapshot(Base):
    """Faz 17 İŞ 2: günde bir kez alınan durum fotoğrafı — parametreler ve ön koşullar.

    Sağlık raporu canlı probe yapmaz; ama "DÜN'e göre DEĞİŞEN parametreler" (biri elle
    değişiklik yaptıysa görünsün) ve "ön koşul eksikliği yüzünden yapılamayan analizler"
    soruları geçmişe dönük veri ister. `pg_settings` ve ön koşul denetimi 15 saniyelik toplama
    döngüsüne konulamayacak kadar pahalı; bunun yerine zaten günde bir kez çalışan rollup işine
    eklendi (şema taramasıyla aynı desen).

    `kind`: "parameters" | "prerequisites". `payload` ilgili servisin ham çıktısı.
    """

    __tablename__ = "daily_state_snapshots"
    __table_args__ = (
        UniqueConstraint("instance_id", "kind", "day", name="uq_daily_state_instance_kind_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
