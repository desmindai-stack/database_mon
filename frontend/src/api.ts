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
  options?: ClusterServiceOptions | null;
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

export interface AlertRule {
  id: number;
  instance_id: number | null;
  name: string;
  metric: string;
  operator: string;
  threshold: number;
  enabled: boolean;
  created_at: string;
}

export interface AlertEvent {
  id: number;
  rule_id: number;
  instance_id: number;
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

const API_BASE = import.meta.env.VITE_API_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  getHealth: () => request<HealthResponse>("/api/health"),
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
    request<{ ok: boolean; message: string; details: Record<string, unknown> }>(
      "/api/instances/test",
      { method: "POST", body: JSON.stringify(data) },
    ),
  getMetrics: (id: number, hours = 1) =>
    request<MetricSample[]>(`/api/metrics/${id}?hours=${hours}`),
  getLatestMetrics: (id: number) =>
    request<MetricSample>(`/api/metrics/${id}/latest`),
  getSlowQueries: (id: number) => request<SlowQuery[]>(`/api/queries/${id}`),
  getIndexAdvice: (id: number, query: string) =>
    request<IndexAdvice[]>(`/api/queries/${id}/advice`, {
      method: "POST",
      body: JSON.stringify({ query }),
    }),
  getInsights: (id: number) => request<TuningReport>(`/api/instances/${id}/insights`),
  getActivity: (id: number) => request<ActivitySnapshot>(`/api/instances/${id}/activity`),
  getClusterHealth: (id: number) => request<ClusterHealth>(`/api/instances/${id}/cluster-health`),
  getClusterLogs: (id: number, service: string, lines = 100) =>
    request<ClusterLogs>(`/api/instances/${id}/cluster-logs?service=${encodeURIComponent(service)}&lines=${lines}`),
  getSchemaHealth: (id: number) => request<SchemaHealth>(`/api/instances/${id}/schema-health`),
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
  createAlertRule: (data: Omit<AlertRule, "id" | "created_at">) =>
    request<AlertRule>("/api/alerts/rules", { method: "POST", body: JSON.stringify(data) }),
  deleteAlertRule: (id: number) =>
    request<void>(`/api/alerts/rules/${id}`, { method: "DELETE" }),
  getAlertEvents: () => request<AlertEvent[]>("/api/alerts/events"),
  resolveAlert: (id: number) =>
    request<AlertEvent>(`/api/alerts/events/${id}/resolve`, { method: "POST" }),
  getPredictions: () => request<Prediction[]>("/api/predictions"),
  ackPrediction: (id: number) =>
    request<Prediction>(`/api/predictions/${id}/ack`, { method: "POST" }),
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

export const ENGINE_DEFAULTS: Record<DbEngine, { port: number; database: string }> = {
  postgresql: { port: 5432, database: "postgres" },
  sqlserver: { port: 1433, database: "master" },
  mongodb: { port: 27017, database: "admin" },
};
