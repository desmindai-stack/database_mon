from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.engines import DEFAULT_PORTS, DatabaseEngine
from app.domain.topology import CustomerType, GroupEnvironment, GroupTopology, NodeRoleHint, NodeSite, ServerOS, UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    role: str
    is_active: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AccessTokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=255)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: str | None = None
    password: str = Field(min_length=8, max_length=255)
    role: UserRole = UserRole.VIEWER


class UserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


class AdminPasswordResetOut(BaseModel):
    """Returns the generated temporary password once — the user must change it on next login."""

    temporary_password: str


class RetentionStatusOut(BaseModel):
    retention_days: int
    options: list[int]
    last_run_at: str | None = None
    last_deleted_count: int | None = None


class RetentionDaysIn(BaseModel):
    retention_days: int


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: CustomerType = CustomerType.PUBLIC


class CustomerUpdate(BaseModel):
    name: str | None = None
    type: CustomerType | None = None


class CustomerOut(BaseModel):
    id: int
    name: str
    type: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApplicationCreate(BaseModel):
    customer_id: int
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class ApplicationUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ApplicationOut(BaseModel):
    id: int
    customer_id: int
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DatabaseGroupCreate(BaseModel):
    application_id: int
    name: str = Field(min_length=1, max_length=128)
    engine: DatabaseEngine = DatabaseEngine.POSTGRESQL
    topology: GroupTopology = GroupTopology.STANDALONE
    environment: GroupEnvironment = GroupEnvironment.PROD
    access_name: str | None = None
    cluster_name: str | None = None
    vip_address: str | None = None
    listener_port: int | None = None
    notes: str | None = None


class DatabaseGroupUpdate(BaseModel):
    name: str | None = None
    engine: DatabaseEngine | None = None
    topology: GroupTopology | None = None
    environment: GroupEnvironment | None = None
    access_name: str | None = None
    cluster_name: str | None = None
    vip_address: str | None = None
    listener_port: int | None = None
    notes: str | None = None


class ClusterConversionRequest(BaseModel):
    topology: GroupTopology
    access_name: str = Field(min_length=1, max_length=255)
    cluster_name: str = Field(min_length=1, max_length=128)
    vip_address: str | None = None


class WizardClusterOptions(BaseModel):
    """Patroni stack ports, entered once in the wizard and copied onto every node's
    Node.options — see services/cluster_health.py's _node_opts()/probe_node()."""

    patroni_port: int | None = None
    etcd_port: int | None = None
    haproxy_stats_port: int | None = None
    haproxy_stats_path: str | None = None
    keepalived_vip: str | None = None


class WizardNodeInput(BaseModel):
    # Either server_name+host (create a new Server) or existing_server_id (attach to one
    # already registered — e.g. a second named SQL Server instance on a box that already hosts
    # one) must be given; see _validate_server_reference below.
    server_name: str | None = Field(default=None, max_length=128)
    host: str | None = Field(default=None, max_length=255)
    ip_address: str | None = None
    os: ServerOS = ServerOS.LINUX
    site: NodeSite = NodeSite.PRIMARY
    agent_url: str | None = None
    agent_token: str | None = None
    existing_server_id: int | None = None
    instance_name: str | None = None
    port: int
    database: str | None = None
    db_username: str = Field(min_length=1)
    db_password: str = ""
    role_hint: NodeRoleHint = NodeRoleHint.UNKNOWN
    # postgresql only — "disable" (default) or "require"; asyncpg doesn't expose libpq's finer
    # verify-ca/verify-full modes without a manually-built SSLContext (see SORULAR.md).
    ssl_mode: str | None = None
    # postgresql only — None (default) auto-detects a connection pooler (PgBouncer/Supabase
    # pooler) from host/port, True/False overrides the detection explicitly. See
    # collectors/base.py::resolve_uses_pooler.
    uses_pooler: bool | None = None
    # sqlserver only — "sql" (default, username/password) or "windows" (integrated auth, only
    # meaningful if the collector process itself runs on a trusted domain-joined Windows host).
    auth_type: str | None = None
    # mongodb only.
    replica_set: str | None = None
    auth_source: str | None = None

    @model_validator(mode="after")
    def _validate_server_reference(self) -> "WizardNodeInput":
        if self.existing_server_id is None:
            if not self.server_name or not self.server_name.strip():
                raise ValueError("server_name zorunlu (ya da existing_server_id ile mevcut bir sunucu seçin)")
            if not self.host or not self.host.strip():
                raise ValueError("host zorunlu (ya da existing_server_id ile mevcut bir sunucu seçin)")
        return self


class WizardCreateGroupRequest(BaseModel):
    """Everything the one-screen wizard needs to create a group + its servers + instances +
    nodes in a single atomic operation (see routers/wizard.py) — either all of it is created,
    or (on any failure) none of it is."""

    application_id: int
    group_name: str = Field(min_length=1, max_length=128)
    engine: DatabaseEngine
    topology: GroupTopology
    environment: GroupEnvironment = GroupEnvironment.PROD
    access_name: str | None = None
    cluster_name: str | None = None
    vip_address: str | None = None
    listener_port: int | None = None
    notes: str | None = None
    cluster_options: WizardClusterOptions | None = None
    nodes: list[WizardNodeInput] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_topology_shape(self) -> "WizardCreateGroupRequest":
        if self.topology == GroupTopology.PATRONI and self.engine != DatabaseEngine.POSTGRESQL:
            raise ValueError("Patroni topolojisi sadece PostgreSQL için geçerli")
        if self.topology == GroupTopology.ALWAYSON and self.engine != DatabaseEngine.SQLSERVER:
            raise ValueError("Always On topolojisi sadece SQL Server için geçerli")
        # MongoDB has no cluster/replica-set topology modeled in dbace yet (GroupTopology is
        # {standalone, patroni, alwayson}) — only a single-node group is meaningful today.
        if self.engine == DatabaseEngine.MONGODB and self.topology != GroupTopology.STANDALONE:
            raise ValueError("MongoDB için şu anda sadece standalone topoloji destekleniyor")

        if self.topology == GroupTopology.STANDALONE:
            if len(self.nodes) != 1:
                raise ValueError("Standalone topoloji tam olarak 1 düğüm gerektirir")
        else:
            if not (2 <= len(self.nodes) <= 8):
                raise ValueError("Cluster topolojisi 2-8 arası düğüm gerektirir")
            if not self.access_name:
                raise ValueError("Cluster grupları için erişim adı (listener/VIP) zorunludur")
            if not self.cluster_name:
                raise ValueError("Cluster grupları için cluster adı zorunludur")
        return self


class WizardAddNodesRequest(BaseModel):
    """Same one-screen wizard, opened in "add node(s) to an existing group" mode instead of
    "create a new group" — engine/topology/cluster info all come from the group already, only
    the node list is new. See POST /api/wizard/groups/{group_id}/nodes."""

    nodes: list[WizardNodeInput] = Field(min_length=1, max_length=8)
    # If omitted, an existing sibling node's options (Patroni ports etc.) are reused so the
    # user doesn't have to re-enter cluster-wide settings just to add one more replica.
    cluster_options: WizardClusterOptions | None = None


class GroupStatusSummaryOut(BaseModel):
    overall: str = "unknown"
    nodes_up: int = 0
    nodes_down: int = 0
    primary_node: str | None = None
    replication_lag_bytes: float | None = None
    checked_at: datetime | None = None


class DatabaseGroupOut(BaseModel):
    id: int
    application_id: int
    name: str
    engine: str
    topology: str
    environment: str
    access_name: str | None
    cluster_name: str | None
    vip_address: str | None
    listener_port: int | None = None
    notes: str | None
    created_at: datetime
    status: GroupStatusSummaryOut | None = None

    model_config = {"from_attributes": True}


class ServerCreate(BaseModel):
    customer_id: int
    name: str = Field(min_length=1, max_length=128)
    host: str
    ip_address: str | None = None
    os: ServerOS = ServerOS.LINUX
    site: NodeSite = NodeSite.PRIMARY
    agent_url: str | None = None
    agent_token: str | None = None


class ServerUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    ip_address: str | None = None
    os: ServerOS | None = None
    site: NodeSite | None = None
    agent_url: str | None = None
    agent_token: str | None = None


class ServerAgentTestRequest(BaseModel):
    agent_url: str
    agent_token: str | None = None


class ServerOut(BaseModel):
    id: int
    customer_id: int
    name: str
    host: str
    ip_address: str | None = None
    os: str
    site: str
    agent_url: str | None
    agent_token: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class NodeCreate(BaseModel):
    group_id: int
    server_id: int
    name: str = Field(min_length=1, max_length=128)
    instance_name: str | None = None
    port: int
    role_hint: NodeRoleHint = NodeRoleHint.UNKNOWN
    options: dict[str, Any] | None = None
    # Instance linkage: either point at an existing Instance, or supply
    # db_username (+ optional password/database) to auto-create one for this node.
    instance_id: int | None = None
    db_username: str | None = None
    db_password: str | None = None
    db_database: str | None = None


class NodeUpdate(BaseModel):
    server_id: int | None = None
    name: str | None = None
    instance_name: str | None = None
    port: int | None = None
    role_hint: NodeRoleHint | None = None
    options: dict[str, Any] | None = None
    instance_id: int | None = None
    db_username: str | None = None
    db_password: str | None = None
    db_database: str | None = None


class NodeOut(BaseModel):
    id: int
    group_id: int
    server_id: int | None
    name: str
    instance_name: str | None
    port: int
    role_hint: str
    options: dict[str, Any] | None = None
    instance_id: int | None = None
    created_at: datetime
    # Read-only display convenience, derived from node.server (host/site/ip_address now live
    # on Server, not Node) — never accepted on create/update, just filled in by the router.
    host: str | None = None
    site: str | None = None
    ip_address: str | None = None

    model_config = {"from_attributes": True}


class LinkedNodeOut(BaseModel):
    """Instance'a bağlı bir cluster düğümü — silme onayında gösterilir (Faz 16-B İŞ 2)."""

    id: int
    name: str
    group_id: int
    port: int


class InstanceDependenciesOut(BaseModel):
    """Instance silinirken birlikte silinecek kayıtların sayımı.

    Silme daha önce foreign key kısıtlarına takılıp hata veriyordu; artık kullanıcı neyi
    kaybedeceğini görüp "birlikte sil" (cascade) diyebiliyor.
    """

    instance_id: int
    metric_samples: int = 0
    slow_query_samples: int = 0
    alert_rules: int = 0
    alert_events: int = 0
    predictions: int = 0
    metric_rollups: int = 0
    schema_object_samples: int = 0
    # Faz 23: bu iki alan eksikti — `prediction_outcomes` (Faz 20) ve `daily_state_snapshots`
    # (Faz 17) sayıma hiç girmiyordu, dolayısıyla "bağlı kayıt yok" denip silme 500 veriyordu.
    prediction_outcomes: int = 0
    daily_state_snapshots: int = 0
    total_records: int = 0
    # Düğümler silinmez, sadece bağlantıları koparılır — ayrı listelenmelerinin sebebi bu.
    linked_nodes: list[LinkedNodeOut] = []
    # Tablo bazında tam döküm — sayım artık model metadata'sından türetildiği için isim
    # listesi sabit değil; arayüz bunu olduğu gibi gösterebilir.
    breakdown: dict[str, int] = {}


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
    group_id: int | None = None
    # Per-instance override of the global collect_interval_seconds — null means "use the
    # app-wide default". Lets a lower-priority/less critical server be monitored less often.
    collect_interval_seconds: int | None = Field(default=None, ge=5, le=3600)

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
    group_id: int | None = None
    enabled: bool | None = None
    collect_interval_seconds: int | None = Field(default=None, ge=5, le=3600)


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
    group_id: int | None = None
    options: dict[str, Any] | None = None
    collect_interval_seconds: int | None = None
    # Collector-derived (see Instance model docstring) — null until the first successful
    # collect_metrics() run for this instance.
    server_version: str | None = None
    unsupported_metrics: dict[str, str] | None = None

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
    # Faz 18 İŞ 1: sorgunun kararlı kimliği. queryid NULL gelebildiği için (ayrıcalıksız rolde
    # pg_stat_statements maskeler) tek başına queryid'ye güvenilemiyor; rapor derin bağlantısı
    # da bu anahtarla eşleşiyor.
    key: str = ""
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
    # Faz 18 İŞ 2: sistem/platform sorgusu mu, öyleyse hangi kurala takıldı.
    is_system: bool = False
    system_reason: str | None = None
    # Pencerede kaç örnek görüldü — 1 ise fark hesaplanamamıştır.
    sample_count: int = 0

    model_config = {"from_attributes": True}


class SlowQueryListOut(BaseModel):
    """Yavaş sorgu listesi + pencerenin kendisi hakkında bilgi (Faz 18 İŞ 1).

    Düz bir liste yerine zarflanmış bir yanıt: arayüzün "hangi pencereye bakıyorum",
    "fark mı anlık görüntü mü" ve "kaç sorgu filtrelendi" sorularını cevaplayabilmesi için.
    Rapor ile DPA'nın aynı veriyi gösterdiğini kullanıcıya kanıtlayan bilgi de bu.
    """

    items: list[SlowQueryOut] = []
    # "delta" | "snapshot" — pencerede tek toplama döngüsü varsa fark alınamaz.
    mode: str = "delta"
    window_start: datetime | None = None
    window_end: datetime | None = None
    filtered_system: int = 0
    filtered_insignificant: int = 0


class QueryDiagnosisOut(BaseModel):
    queryid: str | None
    query: str
    calls: int
    mean_time_ms: float
    total_time_ms: float
    resource: str  # io | cpu | memory | lock | unknown
    reason: str
    confidence: str  # observed | inferred


class QueryDiagnosticsReportOut(BaseModel):
    generated_at: datetime
    limit: int
    diagnoses: list[QueryDiagnosisOut]
    by_resource: dict[str, int]
    agent_configured: bool
    server_resource_note: str


class AlertRuleCreate(BaseModel):
    instance_id: int | None = None
    group_id: int | None = None
    name: str
    rule_type: Literal["metric", "custom"] = "metric"
    metric: str | None = None
    operator: str
    threshold: float
    enabled: bool = True
    severity: str = "warning"
    engine: str | None = None
    sql_query: str | None = None
    interval_seconds: int = Field(default=60, ge=10, le=3600)


class AlertRuleUpdate(BaseModel):
    """Applies to custom rules only — default (auto-created) rules only accept threshold/
    enabled changes, enforced in the router, not here."""

    name: str | None = None
    metric: str | None = None
    operator: str | None = None
    threshold: float | None = None
    enabled: bool | None = None
    severity: str | None = None
    engine: str | None = None
    sql_query: str | None = None
    interval_seconds: int | None = Field(default=None, ge=10, le=3600)


class AlertRuleOut(BaseModel):
    id: int
    instance_id: int | None
    group_id: int | None = None
    name: str
    rule_type: str
    metric: str
    operator: str
    threshold: float
    enabled: bool
    is_default: bool
    severity: str
    engine: str | None
    sql_query: str | None
    interval_seconds: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertEventOut(BaseModel):
    id: int
    rule_id: int
    instance_id: int | None
    group_id: int | None = None
    metric_value: float
    message: str
    triggered_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class CustomRuleTestRequest(BaseModel):
    sql_query: str = Field(min_length=1)
    instance_id: int | None = None
    group_id: int | None = None


# --- Standart öneri yapısı (Faz 17 Ek İŞ B) ---
# Bu iki model, kendisini KULLANAN her modelden önce tanımlı olmak zorunda: Python <= 3.13
# sınıf gövdesindeki annotation'ı hemen değerlendirir, sonra tanımlanan bir isim import anında
# NameError verir. (Python 3.14 PEP 649 ile ertelemeli değerlendirdiğinden bu hata geliştirme
# makinesinde görünmez; bkz. tests/test_definition_order.py.)


class AdviceStepOut(BaseModel):
    action: str
    command: str | None = None


class AdviceOut(BaseModel):
    """Standart öneri yapısı (Faz 17 Ek İŞ B) — rapor, dashboard, DPA ve tahminlerde AYNI şekil.

    Öneri üretilemiyorsa `unavailable_reason` dolu gelir; boş bir öneri hiçbir zaman dönmez.
    """

    title: str
    why: str = ""
    steps: list[AdviceStepOut] = []
    cautions: list[str] = []
    estimated_duration: str | None = None
    rollback: str | None = None
    verification: str | None = None
    unavailable_reason: str | None = None

    @field_validator("steps", "cautions", mode="before")
    @classmethod
    def _lists_never_null(cls, v):
        """`advice` serbest biçimli bir JSON kolonunda saklanıyor; eski bir kayıtta bu anahtar
        `null` olabilir. Varsayılan yalnızca anahtar EKSİKSE devreye girer — açıkça `null` ise
        Pydantic doğrulama hatası verir ve raporun TAMAMI 500 döner. Boşa çeviriyoruz."""
        return v or []


class PredictionStepOut(BaseModel):
    """Tahmin için tek bir çözüm adımı (Faz 16-B İŞ 7). Arayüzde numaralanır; `command` varsa
    ayrı satırda, kopyalanabilir bir kutuda gösterilir."""

    title: str
    detail: str
    command: str | None = None


class PredictionAccuracyOut(BaseModel):
    """Bir tahmin türünün ölçülmüş doğruluğu (Faz 20 İŞ 2).

    `confidence` ile karıştırılmamalı: o, regresyonun geçmiş veriye oturma iyiliğidir (R²).
    Buradakiler tahminin GERÇEKLEŞENE ne kadar yaklaştığını söyler.
    """

    kind: str
    label: str
    evaluated_count: int
    pending_count: int
    expired_count: int
    mean_absolute_error: float | None = None
    mean_percent_error: float | None = None
    interval_hit_rate: float | None = None
    reliability: str
    window_days: int
    note: str


class PredictionReliabilityOut(BaseModel):
    """Tek bir tahmine iliştirilen güvenilirlik işareti."""

    level: str  # "unknown" | "low" | "medium" | "high"
    interval_hit_rate: float | None = None
    evaluated_count: int = 0
    note: str = ""


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
    recommendation: str | None = None
    action: str | None = None
    acknowledged_at: datetime | None
    lower_bound: float | None = None
    upper_bound: float | None = None
    seasonality: str | None = None
    # Faz 17 Ek İŞ B: rapor/dashboard/DPA ile aynı öneri yapısı. `playbook` ham adım listesi
    # olarak duruyor (geriye dönük uyumluluk); `advice` onun standart hali.
    advice: AdviceOut | None = None
    # Adım adım çözüm planı — `recommendation` özetinin açılımı. Veritabanında NULL olabilir
    # (planı olmayan tahmin türleri ve bu alan eklenmeden önce kaydedilmiş satırlar), bu yüzden
    # None boş listeye çevriliyor — istemci her zaman bir dizi görüyor.
    playbook: list[PredictionStepOut] = []
    # Faz 20 İŞ 2: bu TÜRÜN ölçülmüş doğruluğu. Tahminin kendisine değil ailesine ait — "bu tür
    # tahminler son 30 günde ne kadar tuttu" sorusunun cevabı. Router dolduruyor.
    reliability: PredictionReliabilityOut | None = None
    # Faz 20 İŞ 3 — yöntem şeffaflığı. `confidence` (R²) tek başına yanıltıcıydı: modelin
    # geçmişe oturma iyiliğini söyler, verinin doğrusal modele UYUP uymadığını değil.
    method: str | None = None
    sample_count: int | None = None
    span_days: float | None = None
    outliers_removed: int | None = None
    fit_kind: str | None = None
    fit_note: str | None = None
    eta_days_min: float | None = None
    eta_days_max: float | None = None

    @field_validator("playbook", mode="before")
    @classmethod
    def _playbook_never_null(cls, v):
        return v or []

    @model_validator(mode="after")
    def _build_advice(self):
        """Faz 17 Ek İŞ B: playbook'u standart öneri yapısına çevirir.

        Dönüşüm sunum anında yapılıyor, veritabanına ikinci bir kopya yazılmıyor: playbook
        zaten tahminle birlikte kaydedilmiş durumda ve iki kopya zamanla ayrışırdı.
        """
        if self.advice is not None:
            return self
        if not self.playbook:
            self.advice = AdviceOut(
                title=self.recommendation or "Aksiyon planı yok",
                why=self.message,
                unavailable_reason=(
                    None
                    if self.recommendation
                    else "Bu tahmin türü için adım adım plan üretilmiyor; çözüm sunucuya ve iş yüküne "
                    "özgü olduğundan genel bir komut listesi yanıltıcı olurdu."
                ),
            )
            return self

        verification = _PREDICTION_VERIFICATION.get(self.metric_key.split(":")[0])
        self.advice = AdviceOut(
            title=self.recommendation or "Kapasite riskini giderin",
            why=self.message,
            steps=[
                AdviceStepOut(
                    action=f"{step.title}: {step.detail}".strip(": ").strip(),
                    command=step.command,
                )
                for step in self.playbook
            ],
            cautions=[
                "Adımlardaki VACUUM FULL / REINDEX / max_connections değişikliği gibi komutlar "
                "kilitleme ya da yeniden başlatma gerektirebilir; her adımın kendi açıklamasını okuyun."
            ],
            verification=verification,
        )
        return self

    model_config = {"from_attributes": True}


# Tahmin türüne göre "düzeldi mi?" sorgusu — uygulandıktan sonra çalıştırılacak kontrol.
_PREDICTION_VERIFICATION = {
    "database_size_bytes": "SELECT pg_size_pretty(pg_database_size(current_database())) AS boyut;",
    "transaction_id_age": (
        "SELECT datname, age(datfrozenxid) AS yas FROM pg_database ORDER BY yas DESC;"
    ),
    "connection_utilization_pct": (
        "SELECT count(*) AS toplam,\n"
        "       (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS azami\n"
        "FROM pg_stat_activity;"
    ),
    "active_connections": (
        "SELECT count(*) AS toplam,\n"
        "       (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS azami\n"
        "FROM pg_stat_activity;"
    ),
    "table_growth": (
        "SELECT pg_size_pretty(pg_total_relation_size('<sema>.<tablo>')) AS toplam_boyut;"
    ),
    "index_bloat": (
        "SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) AS boyut, idx_scan\n"
        "FROM pg_stat_user_indexes ORDER BY pg_relation_size(indexrelid) DESC LIMIT 10;"
    ),
}


class PredictionReadinessOut(BaseModel):
    kind: str
    label: str
    have_days: float
    need_days: float
    have_samples: int
    need_samples: int
    ready: bool
    days_remaining: float
    note: str = ""


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


class ConfigOut(BaseModel):
    deployment_mode: str
    default_customer_name: str | None


class DashboardTotalsOut(BaseModel):
    customers: int = 0
    applications: int = 0
    groups: int = 0
    nodes: int = 0


class DashboardHealthOut(BaseModel):
    critical: int = 0
    warning: int = 0
    healthy: int = 0
    unknown: int = 0


class DashboardRecommendationOut(BaseModel):
    severity: str
    source: str
    group: str
    message: str
    # Short imperative "Öneri: <title>" headline (Faz 16 İŞ 2) — distinct from `message` (the
    # diagnostic/problem statement shown on the card's collapsed head) and from `steps` (the
    # fuller walk-through). Optional so a recommendation generated before this field existed
    # still renders (frontend falls back to a generic label).
    title: str | None = None
    # Numbered walk-through shown when the card's "çözüm önerisi" section is expanded (Faz 15
    # İŞ 4) — falls back to an empty list for any recommendation generated before this existed.
    steps: list[str] = []
    # Separate, single-line, copy-pasteable follow-up command — not every recommendation has
    # one (e.g. prose-only performance_insights findings), so this stays optional.
    action: str | None = None
    # Faz 17 Ek İŞ B: standart öneri yapısı — rapor bulgularıyla AYNI şekil. `title`/`steps`/
    # `action` alanları geriye dönük uyumluluk için duruyor; arayüz bu alanı tercih ediyor.
    advice: AdviceOut | None = None
    customer: str = ""
    application: str = ""
    environment: str = ""
    link_hint: str = ""
    checked_at: datetime | None = None


class DashboardIssueOut(BaseModel):
    severity: str
    customer: str
    application: str
    group: str
    node: str | None = None
    environment: str
    message: str
    link_hint: str
    checked_at: datetime | None = None
    recommendation: DashboardRecommendationOut | None = None


class GroupStatusRowOut(BaseModel):
    """One row per database group regardless of status — unlike top_issues (only groups with
    an active problem), this is what the dashboard's clickable stat-card filter (Faz 15 İŞ 3)
    actually filters, since "healthy"/"unknown" groups have no issue to show otherwise."""

    group_id: int
    group: str
    customer: str
    application: str
    environment: str
    status: str
    link_hint: str


class DashboardSummaryOut(BaseModel):
    totals: DashboardTotalsOut
    health: DashboardHealthOut
    top_issues: list[DashboardIssueOut]
    recommendations: list[DashboardRecommendationOut]
    groups: list[GroupStatusRowOut] = []
    last_checked: datetime | None = None


class RefreshIntervalOut(BaseModel):
    seconds: int
    options: list[int]


class RefreshIntervalIn(BaseModel):
    seconds: int


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = {}


class PrerequisiteCheckOut(BaseModel):
    key: str
    name: str
    status: str  # ok | partial | missing | unauthorized | unknown
    severity: str  # high | medium
    impact: str
    fix: str | None = None
    detail: str | None = None
    # Faz 16-B İŞ 6: kullanıcı bu kontrolü "ortamımda geçerli değil" diye işaretlediyse true.
    # Durum (status) yine gerçek sonucu gösterir — yoksaymak kontrolü yeşile boyamaz, sadece
    # ilerleme yüzdesinden ve dashboard uyarılarından çıkarır.
    ignored: bool = False


class PrerequisiteReportOut(BaseModel):
    engine: str
    checked_at: datetime
    checks: list[PrerequisiteCheckOut]
    ok_count: int
    issue_count: int
    # Yoksayılanlar hariç tamamlanma yüzdesi — kalan zorunlu kontroller bittiğinde %100 olur.
    ignored_count: int = 0
    completion_pct: int = 0


class IgnoredPrerequisitesUpdate(BaseModel):
    """Yoksayılan ön koşul anahtarlarının TAM listesi (idempotent)."""

    keys: list[str] = []


class SlowQueryAvailabilityOut(BaseModel):
    """Faz 16-B İŞ 1: "yavaş sorgu verisi neden yok?" — tek kaynak.

    Frontend'deki bütün boş-liste mesajları bunu gösteriyor; sabit "eklentiyi kurun" metni
    kaldırıldı (ön koşul paneliyle çelişebiliyordu).
    """

    status: str
    title: str
    message: str
    fix: str | None = None
    stored_samples: int = 0
    last_collected_at: datetime | None = None
    server_rows: int | None = None
    redacted_rows: int | None = None
    ignored_prerequisite: str | None = None


class MetricDefinitionOut(BaseModel):
    key: str
    display_name: str
    unit: str
    category: str
    engines: list[str]
    description: str


class IndexAdviceRequest(BaseModel):
    query: str = Field(min_length=1)
    # pg_stat_statements.calls for this query, if the caller has it (Faz 16 İŞ 4) — used to warn
    # when a recommendation would be based on very few executions.
    calls: int | None = None


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
    # Faz 17 Ek İŞ B: rapor ve dashboard ile AYNI öneri yapısı — arayüzde tek bileşen.
    advice: AdviceOut | None = None


class NoAdviceReasonOut(BaseModel):
    code: str
    message: str
    what_to_do: str


class IndexAdviceReportOut(BaseModel):
    advice: list[IndexAdviceOut]
    no_advice_reasons: list[NoAdviceReasonOut]


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
    # Faz 16-B İŞ 5: severity filtresi üç listede de çalışsın diye.
    severity: str = "medium"


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
    # Faz 16-B İŞ 5: kopyalanabilir, tam ve çalıştırılabilir komut.
    vacuum_ddl: str = ""


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
    vacuum_ddl: str = ""


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


class ClusterServiceStatusOut(BaseModel):
    service: str
    status: str
    latency_ms: float | None = None
    detail: str = ""
    source: str = "probe"
    checked_at: str | None = None
    role: str | None = None
    state: str | None = None
    up_backends: int | None = None
    down_backends: int | None = None
    vip: str | None = None
    vip_owner_local: bool | None = None
    systemd_active: str | None = None
    patroni_version: str | None = None


class ClusterMemberOut(BaseModel):
    name: str | None = None
    role: str | None = None
    state: str | None = None
    host: str | None = None
    lag: float | None = None


class ClusterSummaryOut(BaseModel):
    leader: str | None = None
    members: list[ClusterMemberOut] = []
    member_count: int = 0
    has_leader: bool = False


class ClusterAgentInfoOut(BaseModel):
    configured: bool = False
    reachable: bool = False
    url: str | None = None


class ClusterTotalsOut(BaseModel):
    up: int = 0
    down: int = 0
    unknown: int = 0
    skipped: int = 0


class ClusterHealthOut(BaseModel):
    instance_id: int
    cluster_name: str | None = None
    overall: str
    checked_at: str
    services: list[ClusterServiceStatusOut]
    cluster: ClusterSummaryOut | None = None
    agent: ClusterAgentInfoOut
    totals: ClusterTotalsOut


class ClusterLogsOut(BaseModel):
    service: str
    unit: str | None = None
    lines: list[str] = []
    error: str | None = None


class NodeHealthOut(BaseModel):
    node_id: int
    node_name: str
    site: str
    role_hint: str
    services: list[ClusterServiceStatusOut]
    agent: ClusterAgentInfoOut


class EtcdQuorumOut(BaseModel):
    total: int = 0
    up: int = 0
    quorum_size: int = 0
    has_quorum: bool = True


class DownNodeOut(BaseModel):
    node_name: str
    site: str


class GroupHealthOut(BaseModel):
    group_id: int
    group_name: str
    topology: str
    overall: str
    checked_at: str
    nodes: list[NodeHealthOut]
    cluster: ClusterSummaryOut | None = None
    etcd_quorum: EtcdQuorumOut
    split_brain: bool
    split_brain_nodes: list[str] = []
    down_nodes: list[DownNodeOut] = []
    totals: ClusterTotalsOut


class ParameterFindingOut(BaseModel):
    name: str
    category: str
    current_value: str | None
    unit: str | None
    severity: str
    recommendation: str
    detail: str


class ParameterAuditSummaryOut(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    ok: int = 0
    unknown: int = 0


class ParameterAuditOut(BaseModel):
    group_id: int
    group_name: str
    node_id: int
    node_name: str
    checked_at: str
    findings: list[ParameterFindingOut]
    patroni_config: dict[str, Any] | None = None
    summary: ParameterAuditSummaryOut


class ReplicaDatabaseOut(BaseModel):
    database_name: str | None = None
    synchronization_state: str | None = None
    sync_health: str | None = None
    log_send_queue_kb: float | None = None
    redo_queue_kb: float | None = None
    last_commit_time: str | None = None


class ReplicaHealthOut(BaseModel):
    node_id: int | None = None
    node_name: str | None = None
    replica_server_name: str
    site: str | None = None
    role: str | None = None
    operational_state: str | None = None
    connected_state: str | None = None
    sync_health: str | None = None
    availability_mode: str | None = None
    failover_mode: str | None = None
    failover_ready: bool = False
    databases: list[ReplicaDatabaseOut] = []


class AlwaysOnHealthOut(BaseModel):
    group_id: int
    group_name: str
    ag_name: str | None = None
    primary_replica: str | None = None
    ag_sync_health: str | None = None
    overall: str
    checked_at: str
    replicas: list[ReplicaHealthOut]


# --- Sağlık Raporu (Faz 17) ---


class FindingFactOut(BaseModel):
    """Bulgunun sayısal özetinden tek satır (Faz 18 İŞ 4): "ne kadar / neye göre".

    `tone` arayüzde vurgu rengini belirler; bilinmeyen bir değer gelirse (eski kayıt, elle
    yazılmış veri) "neutral" kabul edilir — geçersiz bir ton yüzünden tüm rapor patlamamalı.
    """

    label: str = ""
    value: str = ""
    tone: str = "neutral"

    @field_validator("tone", mode="before")
    @classmethod
    def _known_tone(cls, v):
        return v if v in ("neutral", "good", "bad") else "neutral"

    @field_validator("label", "value", mode="before")
    @classmethod
    def _stringify(cls, v):
        return "" if v is None else str(v)


class ReportFindingOut(BaseModel):
    id: int
    section: str
    severity: str
    title: str
    detail: str
    evidence: dict[str, Any] = {}
    recommendation: str | None = None
    commands: list[str] = []
    related_object_type: str | None = None
    related_object_id: int | None = None
    # Faz 18 İŞ 1: bulgunun tam hedefi. Boşsa arayüz bölüm→sekme eşlemesine düşer.
    link_hint: str | None = None
    # Faz 18 İŞ 3/İŞ 4 — GERİLEME DÜZELTMESİ (Faz 20): bu iki alan `ReportFinding` MODELİNDE
    # vardı ve rapor motoru ikisini de yazıyordu, ama bu şemada HİÇ tanımlı değildi. Pydantic
    # tanımsız alanı sessizce kırptığı için API her bulguda `facts`/`note` DÖNDÜRMÜYORDU:
    # arayüzde `finding.facts` her zaman `undefined` oluyor ve `.length` okunduğunda
    # "Cannot read properties of undefined" ile patlıyordu. Yani iki özellik hiç görünmedi.
    note: str | None = None
    facts: list[FindingFactOut] = []
    fingerprint: str
    priority: float
    open_since_days: int
    change_state: str
    acknowledged: bool
    # Ek İŞ A — durum makinesi.
    finding_type: str = ""
    status: str = "open"
    verification_failed: bool = False
    decision_note: str | None = None
    decision_reference: str | None = None
    decision_until: datetime | None = None
    advice: AdviceOut | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_never_null(cls, v):
        return v or {}

    @field_validator("commands", mode="before")
    @classmethod
    def _commands_never_null(cls, v):
        return v or []

    @field_validator("facts", mode="before")
    @classmethod
    def _facts_never_null(cls, v):
        """Kolon nullable ve bu alan eklenmeden ÖNCE üretilmiş raporlarda NULL. İstemci her
        zaman bir dizi görmeli — `null` dönmek arayüzde dizi işlemlerini patlatıyordu."""
        return v or []

    model_config = {"from_attributes": True}


class HealthReportSummaryOut(BaseModel):
    """Rapor listesi satırı — bulgular olmadan (liste ekranı için hafif)."""

    id: int
    scope_type: str
    scope_id: int | None
    scope_label: str
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    generated_by: str
    overall_status: str
    status: str
    progress_pct: int
    progress_label: str | None = None
    error: str | None = None
    duration_ms: int
    previous_report_id: int | None = None
    critical_count: int = 0
    warning_count: int = 0

    model_config = {"from_attributes": True}


class HealthReportOut(HealthReportSummaryOut):
    sections: dict[str, Any] = {}
    findings: list[ReportFindingOut] = []

    @field_validator("sections", mode="before")
    @classmethod
    def _sections_never_null(cls, v):
        return v or {}


class RunReportRequest(BaseModel):
    scope_type: Literal["global", "customer", "application", "group", "instance"] = "global"
    scope_id: int | None = None
    # Rapor dönemi gün cinsinden (1 = günlük, 7 = haftalık, 30 = aylık) ya da özel aralık.
    period_days: int = Field(default=1, ge=1, le=365)
    period_start: datetime | None = None
    period_end: datetime | None = None


class AcknowledgeFindingRequest(BaseModel):
    fingerprint: str
    scope_type: Literal["global", "customer", "application", "group", "instance"] = "global"
    scope_id: int | None = None
    # Varsayılan 30 gün; None = süresiz (arayüz bunu bilinçli bir seçim olarak sunar).
    expires_in_days: int | None = Field(default=30, ge=1, le=365)
    note: str | None = None


class FindingAcknowledgementOut(BaseModel):
    id: int
    fingerprint: str
    scope_type: str
    scope_id: int | None
    acknowledged_by: str
    acknowledged_at: datetime
    expires_at: datetime | None
    note: str | None

    model_config = {"from_attributes": True}


class HealthReportScheduleOut(BaseModel):
    hour: int
    enabled: bool
    scope_mode: str
    scope_mode_options: list[str]


class HealthReportScheduleUpdate(BaseModel):
    hour: int | None = Field(default=None, ge=0, le=23)
    enabled: bool | None = None
    scope_mode: Literal["global", "customers", "both"] | None = None


class ExecutiveReportOut(BaseModel):
    """Yönetici raporu (Faz 17 İŞ 3) — müşteriye gösterilen görünüm.

    Teknik raporla aynı veriden türer; teknik alanların (sorgu metni, parametre adı, komut,
    host) hiçbiri bu şemada yoktur — services/executive_report.py bunu üretim sırasında da
    tarayarak zorlar.
    """

    scope_label: str
    period_start: datetime
    period_end: datetime
    period_label: str
    generated_at: datetime
    grade: str
    grade_reason: str
    availability: dict[str, Any] = {}
    inventory: dict[str, Any] = {}
    risks: list[dict[str, Any]] = []
    trend: dict[str, Any] = {}
    work_done: dict[str, Any] = {}
    recommendations: list[dict[str, Any]] = []
    # Ek İŞ A: "planlandı" / "risk kabul" konuları. Yoksayılanlar bu listeye HİÇ girmez.
    decisions: list[dict[str, Any]] = []


# --- Bulgu durum makinesi (Faz 17 Ek İŞ A) ---


class FindingStatusUpdate(BaseModel):
    """Tek bir bulgu için durum kararı.

    `note` zorunlu: notsuz bir susturma kaydı altı ay sonra "bunu neden kapattık?" sorusunu
    cevapsız bırakır. Kapsam varsayılanı en dar seviye (instance).
    """

    fingerprint: str
    finding_type: str
    status: Literal[
        "open", "ignored", "deferred", "risk_accepted", "planned",
        "resolved_pending_verification", "resolved",
    ]
    scope_type: Literal["instance", "group", "application", "customer", "global"] = "instance"
    scope_id: int | None = None
    note: str = Field(min_length=1)
    # "ertelendi"/"yoksayıldı" için bitiş tarihi; geçince bulgu otomatik açılır.
    until: datetime | None = None
    # "planlandı" için serbest metin referans (değişiklik talebi no, ticket no, tarih).
    reference: str | None = None


class BulkFindingStatusUpdate(BaseModel):
    """Toplu işlem: birden çok bulguya aynı durum."""

    findings: list[FindingStatusUpdate] = Field(min_length=1)


class FindingStatusHistoryOut(BaseModel):
    id: int
    fingerprint: str
    finding_type: str | None
    scope_type: str
    scope_id: int | None
    from_status: str | None
    to_status: str
    note: str | None
    reference: str | None
    expires_at: datetime | None
    changed_by: str
    changed_at: datetime

    model_config = {"from_attributes": True}


class FindingDecisionOut(BaseModel):
    id: int
    fingerprint: str
    finding_type: str | None
    scope_type: str
    scope_id: int | None
    status: str
    acknowledged_by: str
    acknowledged_at: datetime
    expires_at: datetime | None
    reference: str | None
    note: str | None

    model_config = {"from_attributes": True}


# --- Gürültü filtresi ayarları (Faz 18 İŞ 2) ---


class NoiseSettingsOut(BaseModel):
    """Rapor ve DPA'nın ORTAK eşikleri. İkisinin farklı eşik kullanması tutarsızlık yaratırdı."""

    list_min_total_ms: float
    list_min_calls: int
    finding_min_total_ms: float
    finding_min_calls: int
    show_system_queries: bool
    defaults: dict[str, Any] = {}


class NoiseSettingsUpdate(BaseModel):
    list_min_total_ms: float | None = Field(default=None, ge=0)
    list_min_calls: int | None = Field(default=None, ge=0)
    finding_min_total_ms: float | None = Field(default=None, ge=0)
    finding_min_calls: int | None = Field(default=None, ge=0)
    show_system_queries: bool | None = None


# --- Veritabanı yükü / bekleme analizi (Faz 25 İŞ 2) ---
#
# TANIM SIRASI: `WaitCategoryShareOut` kendisini KULLANAN modellerden önce tanımlı olmalı.
# Yerel Python 3.14 annotation'ları ertelemeli değerlendirdiği için ters sıra yerelde sessizce
# geçer, canlı Python 3.12'de import anında NameError verir (bkz. tests/test_definition_order.py).


class WaitCategoryShareOut(BaseModel):
    """Tek bir bekleme kategorisinin payı. `label`/`meaning` sunucudan geliyor ki arayüz ile
    rapor aynı sözlüğü konuşsun — iki yerde ayrı çeviri tablosu tutmak, aynı beklemenin iki
    farklı adla görünmesi demekti."""

    category: str
    label: str
    meaning: str
    aas: float
    share_pct: float


class DatabaseLoadPointOut(BaseModel):
    bucket_start: datetime
    total_aas: float
    blocked_aas: float
    # kategori anahtarı -> o kovadaki AAS. Yığılmış alan grafiğinin serisi bu.
    by_category: dict[str, float] = {}


class QueryLoadOut(BaseModel):
    """Bir sorgunun ürettiği yük ve BEKLEME PROFİLİ: süresinin yüzde kaçını nerede geçirdi."""

    queryid: str
    query: str
    aas: float
    share_pct: float
    dominant_category: str | None = None
    dominant_share_pct: float = 0.0
    wait_profile: list[WaitCategoryShareOut] = []


class DatabaseLoadOut(BaseModel):
    instance_id: int
    engine: str
    start: datetime
    end: datetime
    bucket_seconds: int
    samples_taken: int
    average_aas: float
    peak_aas: float
    blocked_aas: float
    series: list[DatabaseLoadPointOut] = []
    categories: list[WaitCategoryShareOut] = []
    top_queries: list[QueryLoadOut] = []
    dominant_category: str | None = None
    dominant_share_pct: float = 0.0
    # "CPU baskın mı, IO baskın mı" sorusunun AÇIK cevabı — kullanıcının grafikten çıkarım
    # yapmasını beklemek yerine cümleyle yazılıyor.
    dominant_verdict: str = ""
    # PostgreSQL 14 öncesinde pg_stat_activity'de query_id yok: bekleme kırılımı var ama
    # sorguya bağlanamıyor. Arayüz bunu söylemeli, boş liste gösterip susmamalı.
    query_attribution_available: bool = True
    # Baskın bekleme tipine göre beş parçalı eylem planı (Faz 25 İŞ 4). Rapor, dashboard, DPA
    # ve tahminlerle AYNI yapı — arayüzde aynı `AdviceCard` bileşeni gösteriyor.
    advice: AdviceOut | None = None
    # Veri yetersizse NEDEN yetersiz olduğu — boş grafik gösterip susmak yasak.
    unavailable_reason: str | None = None
