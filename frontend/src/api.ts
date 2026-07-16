export interface Instance {
  id: number;
  name: string;
  host: string;
  port: number;
  database: string;
  username: string;
  enabled: boolean;
  created_at: string;
}

export interface MetricSample {
  id: number;
  instance_id: number;
  collected_at: string;
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

export interface InstanceCreate {
  name: string;
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
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
  getSummaries: () => request<InstanceSummary[]>("/api/instances/summary"),
  getInstances: () => request<Instance[]>("/api/instances"),
  createInstance: (data: InstanceCreate) =>
    request<Instance>("/api/instances", { method: "POST", body: JSON.stringify(data) }),
  deleteInstance: (id: number) =>
    request<void>(`/api/instances/${id}`, { method: "DELETE" }),
  testConnection: (data: InstanceCreate) =>
    request<{ ok: boolean; message: string; details: Record<string, unknown> }>(
      "/api/instances/test",
      { method: "POST", body: JSON.stringify(data) },
    ),
  getMetrics: (id: number, hours = 1) =>
    request<MetricSample[]>(`/api/metrics/${id}?hours=${hours}`),
  getSlowQueries: (id: number) => request<SlowQuery[]>(`/api/queries/${id}`),
  getAlertRules: () => request<AlertRule[]>("/api/alerts/rules"),
  createAlertRule: (data: Omit<AlertRule, "id" | "created_at">) =>
    request<AlertRule>("/api/alerts/rules", { method: "POST", body: JSON.stringify(data) }),
  deleteAlertRule: (id: number) =>
    request<void>(`/api/alerts/rules/${id}`, { method: "DELETE" }),
  getAlertEvents: () => request<AlertEvent[]>("/api/alerts/events"),
  resolveAlert: (id: number) =>
    request<AlertEvent>(`/api/alerts/events/${id}/resolve`, { method: "POST" }),
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
