export type DbEngine = "postgresql" | "sqlserver" | "mongodb";

export interface ClusterServiceOptions {
  patroni_port?: number;
  etcd_port?: number;
  haproxy_stats_port?: number;
  haproxy_stats_path?: string;
  keepalived_vip?: string | null;
  probe_timeout_sec?: number;
  patroni_tls?: boolean;
  agent_url?: string | null;
  agent_token?: string | null;
  // postgresql only — see collectors/postgresql.py.
  ssl_mode?: string | null;
  // postgresql only — undefined/null = auto-detect a connection pooler (PgBouncer/Supabase
  // pooler) from host/port, true/false overrides it explicitly. See
  // collectors/base.py::resolve_uses_pooler.
  uses_pooler?: boolean | null;
  // sqlserver only — see collectors/sqlserver_mongodb.py::build_odbc_connection_string.
  auth_type?: string | null;
  // mongodb only.
  authSource?: string | null;
  replica_set?: string | null;
}

export interface Instance {
  id: number;
  name: string;
  engine: DbEngine;
  host: string;
  port: number;
  database: string;
  username: string;
  enabled: boolean;
  created_at: string;
  customer_name: string | null;
  environment: string;
  application: string | null;
  cluster_name: string | null;
  role: string | null;
  services: string[] | null;
  group_id: number | null;
  options?: ClusterServiceOptions | null;
  // Null = app-wide default (see RefreshInterval-adjacent settings) — set to sample a
  // lower-priority instance less often.
  collect_interval_seconds: number | null;
  // Collector-derived, read-only — null until the first successful metric collection.
  server_version: string | null;
  unsupported_metrics: Record<string, string> | null;
}

export interface MetricSample {
  id: number;
  instance_id: number;
  collected_at: string;
  metrics: Record<string, number | null>;
  active_connections: number;
  max_connections: number;
  transactions_per_sec: number;
  cache_hit_ratio: number;
  replication_lag_bytes: number | null;
  database_size_bytes: number;
  deadlocks: number;
  temp_bytes: number;
}

export interface InstanceSummary {
  instance: Instance;
  latest_metrics: MetricSample | null;
  status: string;
  alerts_firing: number;
  predictions_open: number;
}

export interface SlowQuery {
  id: number;
  instance_id: number;
  collected_at: string;
  queryid: string | null;
  query: string;
  calls: number;
  total_time_ms: number;
  mean_time_ms: number;
  rows: number;
  shared_blks_hit?: number;
  shared_blks_read?: number;
  local_blks_hit?: number;
  local_blks_read?: number;
  temp_blks_read?: number;
  temp_blks_written?: number;
  plan_user_time?: number;
  plan_sys_time?: number;
  exec_user_time?: number;
  exec_sys_time?: number;
}

export type QueryResourceType = "io" | "cpu" | "memory" | "lock" | "unknown";

export interface QueryDiagnosis {
  queryid: string | null;
  query: string;
  calls: number;
  mean_time_ms: number;
  total_time_ms: number;
  resource: QueryResourceType;
  reason: string;
  confidence: "observed" | "inferred";
}

export interface QueryDiagnosticsReport {
  generated_at: string;
  limit: number;
  diagnoses: QueryDiagnosis[];
  by_resource: Partial<Record<QueryResourceType, number>>;
  agent_configured: boolean;
  server_resource_note: string;
}

export interface IndexAdvice {
  table_name: string;
  schema_name: string;
  columns: string[];
  index_ddl: string;
  reason: string;
  estimated_improvement_pct: number;
  has_hypopg_estimate: boolean;
  before_cost: number | null;
  after_cost: number | null;
  existing_indexes: string[];
}

export interface NoAdviceReason {
  code: string;
  message: string;
  what_to_do: string;
}

export interface IndexAdviceReport {
  advice: IndexAdvice[];
  no_advice_reasons: NoAdviceReason[];
}

export interface PerformanceInsight {
  severity: "critical" | "high" | "medium" | "low" | "info";
  category: string;
  title: string;
  description: string;
  recommendation: string;
  metric_value: number | null;
  metric_unit: string | null;
  action?: string | null;
}

export interface TuningChecklistItem {
  key: string;
  label: string;
  status: "ok" | "warn" | "critical" | "unknown" | string;
  detail: string;
}

export interface TuningReport {
  health_score: number;
  grade: string;
  status: string;
  collected_at: string | null;
  summary: Record<string, number>;
  insights: PerformanceInsight[];
  checklist: TuningChecklistItem[];
}

export interface ActivitySession {
  pid: number;
  usename: string | null;
  datname: string | null;
  application_name: string;
  client_addr: string | null;
  state: string;
  wait_event_type: string | null;
  wait_event: string | null;
  backend_type: string | null;
  query_start: string | null;
  state_change: string | null;
  xact_start: string | null;
  query_duration_sec: number;
  xact_duration_sec: number | null;
  query: string;
  blocking_pids: number[];
  blocked: boolean;
}

export interface ActivitySnapshot {
  sessions: ActivitySession[];
  wait_events: { wait_event_type: string; wait_event: string; count: number }[];
  state_summary: { state: string; count: number }[];
  blocking: {
    blocked_pid: number;
    blocking_pid: number;
    blocked_query: string;
    wait_event_type: string | null;
    wait_event: string | null;
    duration_sec: number;
  }[];
  totals: {
    total: number;
    active: number;
    idle: number;
    idle_in_transaction: number;
    waiting: number;
    blocked: number;
  };
}

export interface ExplainPlanNode {
  node_type: string;
  relation_name: string | null;
  alias: string | null;
  startup_cost: number | null;
  total_cost: number | null;
  plan_rows: number | null;
  plan_width: number | null;
  actual_total_time: number | null;
  actual_rows: number | null;
  shared_hit_blocks: number | null;
  shared_read_blocks: number | null;
  insights: string[];
  children: ExplainPlanNode[];
}

export interface ExplainResult {
  query: string;
  analyzed: boolean;
  planning_time_ms: number | null;
  execution_time_ms: number | null;
  total_cost: number | null;
  insights: string[];
  plan: ExplainPlanNode | null;
  raw_plan: unknown[];
}

export interface QueryHistoryPoint {
  collected_at: string;
  calls: number;
  total_time_ms: number;
  mean_time_ms: number;
  rows: number;
  calls_delta: number | null;
  total_time_delta_ms: number | null;
  interval_mean_ms: number | null;
}

export interface QueryHistorySeries {
  queryid: string;
  query: string;
  points: QueryHistoryPoint[];
  latest_mean_ms: number;
  latest_calls: number;
  max_mean_ms: number;
  min_mean_ms: number;
  avg_mean_ms: number;
  calls_delta_sum: number;
  trend_pct: number;
}

export interface QueryHistoryList {
  hours: number;
  series: QueryHistorySeries[];
}

export interface PrerequisiteCheck {
  key: string;
  name: string;
  status: "ok" | "missing" | "unauthorized" | "unknown";
  severity: "high" | "medium";
  impact: string;
  fix: string | null;
  detail: string | null;
}

export interface PrerequisiteReport {
  engine: string;
  checked_at: string;
  checks: PrerequisiteCheck[];
  ok_count: number;
  issue_count: number;
}

export interface SchemaHealth {
  unused_indexes: {
    schema_name: string;
    table_name: string;
    index_name: string;
    index_bytes: number;
    idx_scan: number;
    idx_tup_read: number;
    idx_tup_fetch: number;
    index_def: string;
    drop_ddl: string;
  }[];
  bloated_tables: {
    schema_name: string;
    table_name: string;
    live_tup: number;
    dead_tup: number;
    dead_ratio_pct: number;
    table_bytes: number;
    last_vacuum: string | null;
    last_autovacuum: string | null;
    last_analyze: string | null;
    last_autoanalyze: string | null;
    freeze_age: number;
    severity: string;
  }[];
  vacuum_lag: {
    schema_name: string;
    table_name: string;
    live_tup: number;
    dead_tup: number;
    last_autovacuum: string | null;
    last_autoanalyze: string | null;
    lag_sec: number;
    freeze_age: number;
    severity: string;
  }[];
  totals: {
    unused_indexes: number;
    unused_index_bytes: number;
    bloated_tables: number;
    vacuum_lag_tables: number;
  };
}

export type AlertRuleType = "metric" | "custom";

export interface AlertRule {
  id: number;
  instance_id: number | null;
  group_id: number | null;
  name: string;
  rule_type: AlertRuleType;
  metric: string;
  operator: string;
  threshold: number;
  enabled: boolean;
  is_default: boolean;
  severity: string;
  engine: string | null;
  sql_query: string | null;
  interval_seconds: number;
  created_at: string;
}

export interface AlertRuleCreate {
  instance_id?: number | null;
  group_id?: number | null;
  name: string;
  rule_type: AlertRuleType;
  metric?: string;
  operator: string;
  threshold: number;
  enabled?: boolean;
  severity?: string;
  engine?: string | null;
  sql_query?: string | null;
  interval_seconds?: number;
}

export interface AlertRuleUpdate {
  name?: string;
  metric?: string;
  operator?: string;
  threshold?: number;
  enabled?: boolean;
  severity?: string;
  engine?: string | null;
  sql_query?: string | null;
  interval_seconds?: number;
}

export interface AlertEvent {
  id: number;
  rule_id: number;
  instance_id: number | null;
  group_id: number | null;
  metric_value: number;
  message: string;
  triggered_at: string;
  resolved_at: string | null;
}

export interface Prediction {
  id: number;
  instance_id: number;
  metric_key: string;
  created_at: string;
  horizon_minutes: number;
  current_value: number;
  predicted_value: number;
  threshold: number;
  confidence: number;
  severity: string;
  message: string;
  recommendation: string | null;
  action: string | null;
  acknowledged_at: string | null;
}

export interface HealthResponse {
  status: string;
  mode: string;
  deployment_mode: "public" | "private";
  default_customer_name: string | null;
  instances: number;
  last_collection: string | null;
}

export interface AppConfig {
  deployment_mode: "public" | "private";
  default_customer_name: string | null;
}

export type CustomerType = "public" | "private";
export type GroupTopology = "standalone" | "patroni" | "alwayson";
export type GroupEnvironment = "prod" | "preprod" | "test" | "dev";
export type NodeSite = "primary" | "disaster";
export type NodeRoleHint = "primary" | "replica" | "unknown";

export interface Customer {
  id: number;
  name: string;
  type: CustomerType;
  created_at: string;
}

export interface CustomerCreate {
  name: string;
  type: CustomerType;
}

export interface Application {
  id: number;
  customer_id: number;
  name: string;
  description: string | null;
  created_at: string;
}

export interface ApplicationCreate {
  customer_id: number;
  name: string;
  description?: string;
}

export interface GroupStatusSummary {
  overall: string;
  nodes_up: number;
  nodes_down: number;
  primary_node: string | null;
  replication_lag_bytes: number | null;
  checked_at: string | null;
}

export interface DatabaseGroup {
  id: number;
  application_id: number;
  name: string;
  engine: DbEngine;
  topology: GroupTopology;
  environment: GroupEnvironment;
  access_name: string | null;
  cluster_name: string | null;
  vip_address: string | null;
  listener_port: number | null;
  notes: string | null;
  created_at: string;
  status: GroupStatusSummary | null;
}

export interface DatabaseGroupCreate {
  application_id: number;
  name: string;
  engine: DbEngine;
  topology: GroupTopology;
  environment?: GroupEnvironment;
  access_name?: string;
  cluster_name?: string;
  vip_address?: string;
  listener_port?: number | null;
  notes?: string;
}

export interface ClusterConversionRequest {
  topology: "alwayson" | "patroni";
  access_name: string;
  cluster_name: string;
  vip_address?: string;
}

export interface WizardClusterOptions {
  patroni_port?: number | null;
  etcd_port?: number | null;
  haproxy_stats_port?: number | null;
  haproxy_stats_path?: string | null;
  keepalived_vip?: string | null;
}

export interface WizardNodeInput {
  // Either server_name+host (create a new Server) or existing_server_id (attach to an
  // already-registered one — e.g. a second named SQL Server instance on a box that already
  // hosts one) must be given.
  server_name?: string;
  host?: string;
  ip_address?: string | null;
  os?: ServerOS;
  site?: NodeSite;
  agent_url?: string | null;
  agent_token?: string | null;
  existing_server_id?: number | null;
  instance_name?: string | null;
  port: number;
  database?: string | null;
  db_username: string;
  db_password: string;
  role_hint: NodeRoleHint;
  // postgresql only.
  ssl_mode?: string | null;
  // postgresql only — undefined/null = auto-detect.
  uses_pooler?: boolean | null;
  // sqlserver only.
  auth_type?: string | null;
  // mongodb only.
  replica_set?: string | null;
  auth_source?: string | null;
}

export interface WizardCreateGroupRequest {
  application_id: number;
  group_name: string;
  engine: DbEngine;
  topology: GroupTopology;
  environment?: GroupEnvironment;
  access_name?: string | null;
  cluster_name?: string | null;
  vip_address?: string | null;
  listener_port?: number | null;
  notes?: string | null;
  cluster_options?: WizardClusterOptions | null;
  nodes: WizardNodeInput[];
}

export interface WizardAddNodesRequest {
  nodes: WizardNodeInput[];
  cluster_options?: WizardClusterOptions | null;
}

export type ServerOS = "linux" | "windows";

export interface DbServer {
  id: number;
  customer_id: number;
  name: string;
  host: string;
  ip_address: string | null;
  os: ServerOS;
  site: NodeSite;
  agent_url: string | null;
  agent_token: string | null;
  created_at: string;
}

export interface ServerCreate {
  customer_id: number;
  name: string;
  host: string;
  ip_address?: string | null;
  os: ServerOS;
  site: NodeSite;
  agent_url?: string;
  agent_token?: string;
}

export interface DbNode {
  id: number;
  group_id: number;
  server_id: number | null;
  name: string;
  instance_name: string | null;
  port: number;
  role_hint: NodeRoleHint;
  options: Record<string, unknown> | null;
  instance_id: number | null;
  created_at: string;
  // Read-only, derived from the linked Server for display convenience.
  host: string | null;
  site: NodeSite | null;
  ip_address: string | null;
}

export interface NodeCreate {
  group_id: number;
  server_id: number;
  name: string;
  instance_name?: string;
  port: number;
  role_hint: NodeRoleHint;
  options?: Record<string, unknown>;
  instance_id?: number | null;
  db_username?: string;
  db_password?: string;
  db_database?: string;
}

export interface NodeServiceStatus {
  service: string;
  status: string;
  latency_ms: number | null;
  detail: string;
  source: string;
  role?: string | null;
  state?: string | null;
  vip_owner_local?: boolean | null;
}

export interface NodeHealth {
  node_id: number;
  node_name: string;
  site: NodeSite;
  role_hint: NodeRoleHint;
  services: NodeServiceStatus[];
  agent: { configured: boolean; reachable: boolean; url: string | null };
}

export interface GroupHealth {
  group_id: number;
  group_name: string;
  topology: GroupTopology;
  overall: string;
  checked_at: string;
  nodes: NodeHealth[];
  cluster: {
    leader: string | null;
    members: { name: string | null; role: string | null; state: string | null; host: string | null; lag: number | null }[];
    member_count: number;
    has_leader: boolean;
  } | null;
  etcd_quorum: { total: number; up: number; quorum_size: number; has_quorum: boolean };
  split_brain: boolean;
  split_brain_nodes: string[];
  down_nodes: { node_name: string; site: string }[];
  totals: { up: number; down: number; unknown: number; skipped: number };
}

export interface ParameterFinding {
  name: string;
  category: string;
  current_value: string | null;
  unit: string | null;
  severity: string;
  recommendation: string;
  detail: string;
}

export interface ParameterAudit {
  group_id: number;
  group_name: string;
  node_id: number;
  node_name: string;
  checked_at: string;
  findings: ParameterFinding[];
  patroni_config: Record<string, unknown> | null;
  summary: Record<string, number>;
}

export interface ReplicaDatabase {
  database_name: string | null;
  synchronization_state: string | null;
  sync_health: string | null;
  log_send_queue_kb: number | null;
  redo_queue_kb: number | null;
  last_commit_time: string | null;
}

export interface ReplicaHealth {
  node_id: number | null;
  node_name: string | null;
  replica_server_name: string;
  site: string | null;
  role: string | null;
  operational_state: string | null;
  connected_state: string | null;
  sync_health: string | null;
  availability_mode: string | null;
  failover_mode: string | null;
  failover_ready: boolean;
  databases: ReplicaDatabase[];
}

export interface AlwaysOnHealth {
  group_id: number;
  group_name: string;
  ag_name: string | null;
  primary_replica: string | null;
  ag_sync_health: string | null;
  overall: string;
  checked_at: string;
  replicas: ReplicaHealth[];
}

export interface InstanceCreate {
  name: string;
  engine: DbEngine;
  host: string;
  port?: number;
  database: string;
  username: string;
  password: string;
  customer_name?: string;
  environment?: string;
  application?: string;
  cluster_name?: string;
  role?: string;
  services?: string[];
  options?: ClusterServiceOptions;
  collect_interval_seconds?: number | null;
}

export interface ClusterServiceStatus {
  service: string;
  status: string;
  latency_ms: number | null;
  detail: string;
  source: string;
  checked_at?: string | null;
  role?: string | null;
  state?: string | null;
  up_backends?: number | null;
  down_backends?: number | null;
  vip?: string | null;
  vip_owner_local?: boolean | null;
  systemd_active?: string | null;
  patroni_version?: string | null;
}

export interface ClusterHealth {
  instance_id: number;
  cluster_name: string | null;
  overall: string;
  checked_at: string;
  services: ClusterServiceStatus[];
  cluster: {
    leader: string | null;
    members: { name: string | null; role: string | null; state: string | null; host: string | null }[];
    member_count: number;
    has_leader: boolean;
  } | null;
  agent: { configured: boolean; reachable: boolean; url: string | null };
  totals: { up: number; down: number; unknown: number; skipped: number };
}

export interface ClusterLogs {
  service: string;
  unit: string | null;
  lines: string[];
  error: string | null;
}

export interface DashboardTotals {
  customers: number;
  applications: number;
  groups: number;
  nodes: number;
}

export interface DashboardHealth {
  critical: number;
  warning: number;
  healthy: number;
  unknown: number;
}

export interface DashboardRecommendation {
  severity: string;
  source: string;
  group: string;
  message: string;
  title: string | null;
  steps: string[];
  action: string | null;
  customer: string;
  application: string;
  environment: string;
  link_hint: string;
  checked_at: string | null;
}

export interface DashboardIssue {
  severity: string;
  customer: string;
  application: string;
  group: string;
  node: string | null;
  environment: GroupEnvironment;
  message: string;
  link_hint: string;
  checked_at: string | null;
  recommendation: DashboardRecommendation | null;
}

export type GroupOverallStatus = "critical" | "warning" | "healthy" | "unknown";

export interface GroupStatusRow {
  group_id: number;
  group: string;
  customer: string;
  application: string;
  environment: GroupEnvironment;
  status: GroupOverallStatus;
  link_hint: string;
}

export interface DashboardSummary {
  totals: DashboardTotals;
  health: DashboardHealth;
  top_issues: DashboardIssue[];
  recommendations: DashboardRecommendation[];
  groups: GroupStatusRow[];
  last_checked: string | null;
}

export interface RefreshInterval {
  seconds: number;
  options: number[];
}

export interface RetentionStatus {
  retention_days: number;
  options: number[];
  last_run_at: string | null;
  last_deleted_count: number | null;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
  details: Record<string, unknown>;
}

export type UserRoleType = "admin" | "viewer";

export interface UserOut {
  id: number;
  username: string;
  email: string | null;
  role: UserRoleType;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface TokenOut {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  user: UserOut;
}

export interface AccessTokenOut {
  access_token: string;
  token_type: "bearer";
}

const API_BASE = import.meta.env.VITE_API_URL ?? "";

// Set by AuthProvider (auth.tsx) on login/logout/refresh — kept as module state rather than
// React state because api.ts is a plain module, not a component, and every request needs the
// current token synchronously.
let authToken: string | null = null;
let refreshTokenValue: string | null = null;
let onUnauthorized: (() => void) | null = null;
let refreshInFlight: Promise<boolean> | null = null;

export function setAuthTokens(access: string | null, refresh: string | null): void {
  authToken = access;
  refreshTokenValue = refresh;
}

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

async function tryRefresh(): Promise<boolean> {
  if (!refreshTokenValue) return false;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshTokenValue }),
        });
        if (!res.ok) return false;
        const data: AccessTokenOut = await res.json();
        authToken = data.access_token;
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

const _PUBLIC_PATHS = new Set(["/api/auth/login", "/api/auth/refresh", "/api/health"]);

async function request<T>(path: string, init?: RequestInit, _retried = false): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init?.headers as Record<string, string> | undefined) };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });

  if (res.status === 401 && !_retried && !_PUBLIC_PATHS.has(path)) {
    if (await tryRefresh()) return request<T>(path, init, true);
    onUnauthorized?.();
    throw new Error("Oturum süresi doldu — tekrar giriş yapın.");
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  login: (username: string, password: string) =>
    request<TokenOut>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logoutRequest: () => request<void>("/api/auth/logout", { method: "POST" }),
  me: () => request<UserOut>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    request<UserOut>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),

  getHealth: () => request<HealthResponse>("/api/health"),
  getConfig: () => request<AppConfig>("/api/config"),
  getSummaries: () => request<InstanceSummary[]>("/api/instances/summary"),
  getInstances: () => request<Instance[]>("/api/instances"),
  getInstance: (id: number) => request<Instance>(`/api/instances/${id}`),
  createInstance: (data: InstanceCreate) =>
    request<Instance>("/api/instances", { method: "POST", body: JSON.stringify(data) }),
  updateInstance: (id: number, data: Partial<InstanceCreate>) =>
    request<Instance>(`/api/instances/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteInstance: (id: number) =>
    request<void>(`/api/instances/${id}`, { method: "DELETE" }),
  testConnection: (data: InstanceCreate) =>
    request<ConnectionTestResult>("/api/instances/test", { method: "POST", body: JSON.stringify(data) }),
  testExistingInstance: (id: number) =>
    request<ConnectionTestResult>(`/api/instances/${id}/test`, { method: "POST" }),
  getMetrics: (id: number, hours = 1) =>
    request<MetricSample[]>(`/api/metrics/${id}?hours=${hours}`),
  getLatestMetrics: (id: number) =>
    request<MetricSample>(`/api/metrics/${id}/latest`),
  getSlowQueries: (id: number) => request<SlowQuery[]>(`/api/queries/${id}`),
  getQueryDiagnostics: (id: number, limit = 10) =>
    request<QueryDiagnosticsReport>(`/api/queries/${id}/diagnostics?limit=${limit}`),
  getIndexAdvice: (id: number, query: string, calls?: number) =>
    request<IndexAdviceReport>(`/api/queries/${id}/advice`, {
      method: "POST",
      body: JSON.stringify({ query, calls: calls ?? null }),
    }),
  getInsights: (id: number) => request<TuningReport>(`/api/instances/${id}/insights`),
  getActivity: (id: number) => request<ActivitySnapshot>(`/api/instances/${id}/activity`),
  getClusterHealth: (id: number) => request<ClusterHealth>(`/api/instances/${id}/cluster-health`),
  getClusterLogs: (id: number, service: string, lines = 100) =>
    request<ClusterLogs>(`/api/instances/${id}/cluster-logs?service=${encodeURIComponent(service)}&lines=${lines}`),
  getSchemaHealth: (id: number) => request<SchemaHealth>(`/api/instances/${id}/schema-health`),
  getPrerequisites: (id: number) => request<PrerequisiteReport>(`/api/instances/${id}/prerequisites`),
  getQueryHistory: (id: number, hours = 24, limit = 10) =>
    request<QueryHistoryList>(`/api/queries/${id}/history?hours=${hours}&limit=${limit}`),
  getQueryHistoryDetail: (id: number, queryid: string, hours = 24) =>
    request<QueryHistorySeries>(`/api/queries/${id}/history/${encodeURIComponent(queryid)}?hours=${hours}`),
  explainQuery: (id: number, query: string, analyze = false) =>
    request<ExplainResult>(`/api/queries/${id}/explain`, {
      method: "POST",
      body: JSON.stringify({ query, analyze }),
    }),
  getAlertRules: () => request<AlertRule[]>("/api/alerts/rules"),
  createAlertRule: (data: AlertRuleCreate) =>
    request<AlertRule>("/api/alerts/rules", { method: "POST", body: JSON.stringify(data) }),
  updateAlertRule: (id: number, data: AlertRuleUpdate) =>
    request<AlertRule>(`/api/alerts/rules/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteAlertRule: (id: number) =>
    request<void>(`/api/alerts/rules/${id}`, { method: "DELETE" }),
  testAlertRuleQuery: (data: { sql_query: string; instance_id?: number; group_id?: number }) =>
    request<ConnectionTestResult>("/api/alerts/rules/test-query", { method: "POST", body: JSON.stringify(data) }),
  getAlertEvents: (activeOnly = true) =>
    request<AlertEvent[]>(`/api/alerts/events?active_only=${activeOnly}`),
  resolveAlert: (id: number) =>
    request<AlertEvent>(`/api/alerts/events/${id}/resolve`, { method: "POST" }),
  getPredictions: () => request<Prediction[]>("/api/predictions"),
  ackPrediction: (id: number) =>
    request<Prediction>(`/api/predictions/${id}/ack`, { method: "POST" }),

  getCustomers: () => request<Customer[]>("/api/customers"),
  createCustomer: (data: CustomerCreate) =>
    request<Customer>("/api/customers", { method: "POST", body: JSON.stringify(data) }),
  updateCustomer: (id: number, data: Partial<CustomerCreate>) =>
    request<Customer>(`/api/customers/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteCustomer: (id: number) => request<void>(`/api/customers/${id}`, { method: "DELETE" }),

  getApplications: (customerId?: number) =>
    request<Application[]>(`/api/applications${customerId ? `?customer_id=${customerId}` : ""}`),
  getApplication: (id: number) => request<Application>(`/api/applications/${id}`),
  createApplication: (data: ApplicationCreate) =>
    request<Application>("/api/applications", { method: "POST", body: JSON.stringify(data) }),
  updateApplication: (id: number, data: Partial<Omit<ApplicationCreate, "customer_id">>) =>
    request<Application>(`/api/applications/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteApplication: (id: number) => request<void>(`/api/applications/${id}`, { method: "DELETE" }),

  getGroups: (applicationId?: number) =>
    request<DatabaseGroup[]>(`/api/groups${applicationId ? `?application_id=${applicationId}` : ""}`),
  getGroup: (id: number) => request<DatabaseGroup>(`/api/groups/${id}`),
  createGroup: (data: DatabaseGroupCreate) =>
    request<DatabaseGroup>("/api/groups", { method: "POST", body: JSON.stringify(data) }),
  updateGroup: (id: number, data: Partial<Omit<DatabaseGroupCreate, "application_id">>) =>
    request<DatabaseGroup>(`/api/groups/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteGroup: (id: number) => request<void>(`/api/groups/${id}`, { method: "DELETE" }),
  convertGroupToCluster: (id: number, data: ClusterConversionRequest) =>
    request<DatabaseGroup>(`/api/groups/${id}/convert-to-cluster`, { method: "POST", body: JSON.stringify(data) }),
  createGroupWizard: (data: WizardCreateGroupRequest) =>
    request<DatabaseGroup>("/api/wizard/database-groups", { method: "POST", body: JSON.stringify(data) }),
  addNodesWizard: (groupId: number, data: WizardAddNodesRequest) =>
    request<DbNode[]>(`/api/wizard/groups/${groupId}/nodes`, { method: "POST", body: JSON.stringify(data) }),

  getGroupNodes: (groupId: number) => request<DbNode[]>(`/api/groups/${groupId}/nodes`),
  createNode: (data: NodeCreate) =>
    request<DbNode>("/api/nodes", { method: "POST", body: JSON.stringify(data) }),
  updateNode: (id: number, data: Partial<Omit<NodeCreate, "group_id">>) =>
    request<DbNode>(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteNode: (id: number) => request<void>(`/api/nodes/${id}`, { method: "DELETE" }),

  getServers: (customerId?: number) =>
    request<DbServer[]>(`/api/servers${customerId ? `?customer_id=${customerId}` : ""}`),
  getServer: (id: number) => request<DbServer>(`/api/servers/${id}`),
  createServer: (data: ServerCreate) =>
    request<DbServer>("/api/servers", { method: "POST", body: JSON.stringify(data) }),
  updateServer: (id: number, data: Partial<Omit<ServerCreate, "customer_id">>) =>
    request<DbServer>(`/api/servers/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteServer: (id: number) => request<void>(`/api/servers/${id}`, { method: "DELETE" }),
  testServerAgent: (agentUrl: string, agentToken?: string) =>
    request<ConnectionTestResult>("/api/servers/test-agent", {
      method: "POST",
      body: JSON.stringify({ agent_url: agentUrl, agent_token: agentToken || undefined }),
    }),
  testExistingServerAgent: (id: number) =>
    request<ConnectionTestResult>(`/api/servers/${id}/test-agent`, { method: "POST" }),
  getServerNodeCount: (id: number) => request<number>(`/api/servers/${id}/node-count`),

  getDashboardSummary: () => request<DashboardSummary>("/api/dashboard/summary"),
  refreshDashboard: () => request<DashboardSummary>("/api/dashboard/refresh", { method: "POST" }),
  getRefreshInterval: () => request<RefreshInterval>("/api/dashboard/refresh-interval"),
  setRefreshInterval: (seconds: number) =>
    request<RefreshInterval>("/api/dashboard/refresh-interval", { method: "PUT", body: JSON.stringify({ seconds }) }),

  getGroupHealth: (groupId: number) => request<GroupHealth>(`/api/groups/${groupId}/health`),
  getGroupParameters: (groupId: number) => request<ParameterAudit>(`/api/groups/${groupId}/parameters`),
  getGroupAlwaysOn: (groupId: number) => request<AlwaysOnHealth>(`/api/groups/${groupId}/alwayson`),

  getRetention: () => request<RetentionStatus>("/api/admin/retention"),
  setRetention: (retention_days: number) =>
    request<RetentionStatus>("/api/admin/retention", { method: "PUT", body: JSON.stringify({ retention_days }) }),
  runRetentionNow: () => request<RetentionStatus>("/api/admin/retention/run", { method: "POST" }),

  getUsers: () => request<UserOut[]>("/api/admin/users"),
  createUser: (data: { username: string; email?: string; password: string; role: UserRoleType }) =>
    request<UserOut>("/api/admin/users", { method: "POST", body: JSON.stringify(data) }),
  updateUser: (id: number, data: { role?: UserRoleType; is_active?: boolean }) =>
    request<UserOut>(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteUser: (id: number) => request<void>(`/api/admin/users/${id}`, { method: "DELETE" }),
  resetUserPassword: (id: number) =>
    request<{ temporary_password: string }>(`/api/admin/users/${id}/reset-password`, { method: "POST" }),
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function formatRelativeTime(iso: string): string {
  const diffSec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (diffSec < 5) return "az önce";
  if (diffSec < 60) return `${Math.floor(diffSec)} saniye önce`;
  const diffMin = diffSec / 60;
  if (diffMin < 60) return `${Math.floor(diffMin)} dakika önce`;
  const diffHour = diffMin / 60;
  if (diffHour < 24) return `${Math.floor(diffHour)} saat önce`;
  return `${Math.floor(diffHour / 24)} gün önce`;
}

export const ENGINE_DEFAULTS: Record<DbEngine, { port: number; database: string }> = {
  postgresql: { port: 5432, database: "postgres" },
  sqlserver: { port: 1433, database: "master" },
  mongodb: { port: 27017, database: "admin" },
};
