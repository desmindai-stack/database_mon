import { Fragment, type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertEvent,
  AlertRule,
  api,
  formatBytes,
  formatTime,
  IndexAdvice,
  Instance,
  InstanceSummary,
  MetricSample,
  Prediction,
  SlowQuery,
} from "../api";

type Tab = "overview" | "metrics" | "queries" | "alerts" | "predictions";
type RangeHours = 1 | 6 | 24 | 168;

const rangeLabel: Record<RangeHours, string> = { 1: "1 saat", 6: "6 saat", 24: "24 saat", 168: "7 gün" };

const statusMeta: Record<string, { label: string; color: string }> = {
  healthy: { label: "Healthy", color: "var(--success)" },
  warning: { label: "Warning", color: "var(--warning)" },
  alerting: { label: "Alerting", color: "var(--danger)" },
  pending: { label: "Pending", color: "var(--muted)" },
  disabled: { label: "Disabled", color: "var(--muted)" },
};

function timeLabel(iso: string): string {
  const d = new Date(iso);
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

function queryFingerprint(q: string): string {
  return q.length > 120 ? q.slice(0, 120) + "…" : q;
}

export default function InstanceDetailPage() {
  const { id } = useParams();
  const instanceId = Number(id);

  const [instance, setInstance] = useState<Instance | null>(null);
  const [summary, setSummary] = useState<InstanceSummary | null>(null);
  const [metrics, setMetrics] = useState<MetricSample[]>([]);
  const [queries, setQueries] = useState<SlowQuery[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [predictions, setPredictions] = useState<Prediction[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [range, setRange] = useState<RangeHours>(6);
  const [querySort, setQuerySort] = useState<"total" | "mean" | "calls">("total");
  const [expandedQuery, setExpandedQuery] = useState<number | null>(null);
  const [advice, setAdvice] = useState<Record<number, IndexAdvice[]>>({});
  const [adviceLoading, setAdviceLoading] = useState<Record<number, boolean>>({});

  const status = summary?.status || "pending";

  const loadAdvice = async (q: SlowQuery) => {
    setAdviceLoading((prev) => ({ ...prev, [q.id]: true }));
    try {
      const result = await api.getIndexAdvice(instanceId, q.query);
      setAdvice((prev) => ({ ...prev, [q.id]: result }));
    } catch (e) {
      setAdvice((prev) => ({ ...prev, [q.id]: [] }));
    } finally {
      setAdviceLoading((prev) => ({ ...prev, [q.id]: false }));
    }
  };

  useEffect(() => {
    if (!instanceId) return;
    let mounted = true;
    const load = async () => {
      try {
        const [inst, m, q, summaries] = await Promise.all([
          api.getInstance(instanceId),
          api.getMetrics(instanceId, range),
          api.getSlowQueries(instanceId),
          api.getSummaries(),
        ]);
        if (!mounted) return;
        setInstance(inst);
        setMetrics(m);
        setQueries(q);
        setSummary(summaries.find((s) => s.instance.id === instanceId) || null);

        const [r, e, p] = await Promise.all([
          api.getAlertRules(),
          api.getAlertEvents(),
          api.getPredictions(),
        ]);
        if (!mounted) return;
        setRules(r.filter((x) => x.instance_id === null || x.instance_id === instanceId));
        setEvents(e.filter((x) => x.instance_id === instanceId));
        setPredictions(p.filter((x) => x.instance_id === instanceId));
      } catch (e) {
        if (mounted) setError(String((e as Error).message || e));
      }
    };
    load();
    const timer = setInterval(() => {
      api.getMetrics(instanceId, range).then(setMetrics).catch(() => undefined);
      api.getSlowQueries(instanceId).then(setQueries).catch(() => undefined);
      api.getSummaries().then((s) => setSummary(s.find((x) => x.instance.id === instanceId) || null)).catch(() => undefined);
    }, 15000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [instanceId, range]);

  const latest = metrics.at(-1);

  const chartData = useMemo(
    () =>
      metrics.map((m) => {
        const metricsJson = m.metrics ?? {};
        return {
          time: timeLabel(m.collected_at),
          connections: m.active_connections,
          maxConnections: m.max_connections,
          cacheHit: m.cache_hit_ratio,
          tps: m.transactions_per_sec,
          size: m.database_size_bytes,
          lag: m.replication_lag_bytes ?? 0,
          deadlocks: m.deadlocks,
          temp: m.temp_bytes,
          blksRead: Number(metricsJson.blks_read_per_sec ?? 0),
          blksHit: Number(metricsJson.blks_hit_per_sec ?? 0),
          tupReturned: Number(metricsJson.tup_returned_per_sec ?? 0),
          tupFetched: Number(metricsJson.tup_fetched_per_sec ?? 0),
          tupInserted: Number(metricsJson.tup_inserted_per_sec ?? 0),
          tupUpdated: Number(metricsJson.tup_updated_per_sec ?? 0),
          tupDeleted: Number(metricsJson.tup_deleted_per_sec ?? 0),
          tempFiles: Number(metricsJson.temp_files_per_sec ?? 0),
          tempBytes: Number(metricsJson.temp_bytes_per_sec ?? 0),
          ioReads: Number(metricsJson.io_reads_per_sec ?? 0),
          ioWrites: Number(metricsJson.io_writes_per_sec ?? 0),
          ioExtends: Number(metricsJson.io_extends_per_sec ?? 0),
          checkpointsTimed: Number(metricsJson.checkpoints_timed ?? 0),
          checkpointsReq: Number(metricsJson.checkpoints_req ?? 0),
          buffersCheckpoint: Number(metricsJson.buffers_checkpoint_per_sec ?? 0),
          buffersBackend: Number(metricsJson.buffers_backend_per_sec ?? 0),
          buffersClean: Number(metricsJson.buffers_clean_per_sec ?? 0),
        };
      }),
    [metrics],
  );

  const sortedQueries = useMemo(() => {
    const list = [...queries];
    if (querySort === "total") list.sort((a, b) => b.total_time_ms - a.total_time_ms);
    if (querySort === "mean") list.sort((a, b) => b.mean_time_ms - a.mean_time_ms);
    if (querySort === "calls") list.sort((a, b) => b.calls - a.calls);
    return list;
  }, [queries, querySort]);

  const topQueriesChart = useMemo(
    () =>
      sortedQueries.slice(0, 10).map((q, i) => ({
        name: `#${i + 1}`,
        total: q.total_time_ms,
        mean: q.mean_time_ms,
        calls: q.calls,
      })),
    [sortedQueries],
  );

  const connectionUtil = latest && latest.max_connections ? (latest.active_connections / latest.max_connections) * 100 : 0;

  const TabButton = ({ value, label }: { value: Tab; label: string }) => (
    <button className={`tab-btn${tab === value ? " active" : ""}`} onClick={() => setTab(value)}>
      {label}
    </button>
  );

  const StatTile = ({ label, value, sub, color }: { label: string; value: string | number; sub?: string; color?: string }) => (
    <div className="card stat-tile" style={{ borderLeftColor: color || "var(--accent)" }}>
      <div className="stat-tile-label">{label}</div>
      <div className="stat-tile-value" style={{ color: color || "var(--text)" }}>{value}</div>
      {sub && <div className="stat-tile-sub">{sub}</div>}
    </div>
  );

  const ChartCard = ({ title, height = 260, children }: { title: string; height?: number; children: ReactNode }) => (
    <div className="card chart-card" style={{ height }}>
      <h3 className="chart-title">{title}</h3>
      <div className="chart-body">{children}</div>
    </div>
  );

  if (!instance) {
    return error ? <div className="error">{error}</div> : <div className="empty">Yükleniyor…</div>;
  }

  return (
    <>
      <header className="page-header detail-header">
        <div>
          <div className="detail-title-row">
            <h2>{instance.name}</h2>
            <span className={`status ${status}`}>{status}</span>
          </div>
          <p className="detail-subtitle">
            <Link to="/instances">← Instances</Link>
            <span className="detail-meta">
              {instance.host}:{instance.port}/{instance.database} · {instance.engine}
            </span>
          </p>
        </div>
        <div className="range-selector">
          {(Object.keys(rangeLabel) as unknown as RangeHours[]).map((h) => (
            <button key={h} className={`range-btn${range === h ? " active" : ""}`} onClick={() => setRange(h)}>
              {rangeLabel[h]}
            </button>
          ))}
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="detail-tabs">
        <TabButton value="overview" label="Overview" />
        <TabButton value="metrics" label="Metrics" />
        <TabButton value="queries" label="Slow Queries" />
        <TabButton value="alerts" label="Alerts" />
        <TabButton value="predictions" label="Predictions" />
      </div>

      {tab === "overview" && (
        <>
          <div className="tags-row">
            {instance.customer_name && <span className="tag">Müşteri: {instance.customer_name}</span>}
            <span className={`tag ${instance.environment}`}>Ortam: {instance.environment}</span>
            {instance.application && <span className="tag">Uygulama: {instance.application}</span>}
            {instance.cluster_name && <span className="tag">Cluster: {instance.cluster_name}</span>}
            {instance.role && <span className="tag">Rol: {instance.role}</span>}
            {(instance.services ?? []).map((s) => (
              <span key={s} className="tag service">{s}</span>
            ))}
          </div>

          <div className="stats-grid compact">
            <StatTile
              label="Connections"
              value={latest ? `${latest.active_connections} / ${latest.max_connections}` : "—"}
              sub={connectionUtil ? `%${connectionUtil.toFixed(1)} kullanım` : ""}
              color={connectionUtil > 85 ? "var(--danger)" : connectionUtil > 60 ? "var(--warning)" : "var(--success)"}
            />
            <StatTile label="Cache hit ratio" value={latest ? `${latest.cache_hit_ratio.toFixed(1)}%` : "—"} color="#22d3ee" />
            <StatTile label="Transactions/sec" value={latest ? latest.transactions_per_sec.toFixed(1) : "—"} color="#a78bfa" />
            <StatTile label="Database size" value={latest ? formatBytes(latest.database_size_bytes) : "—"} color="#f59e0b" />
            <StatTile label="Replication lag" value={latest ? formatBytes(latest.replication_lag_bytes ?? 0) : "—"} color="#f472b6" />
            <StatTile label="Deadlocks" value={latest ? latest.deadlocks : "—"} color="var(--danger)" />
          </div>

          <div className="grid grid-2">
            <ChartCard title="Connections (son 60 dk)">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <defs>
                    <linearGradient id="connGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                      <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                  <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                  <YAxis stroke="#8b9bb8" fontSize={11} />
                  <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                  <ReferenceLine y={latest?.max_connections} stroke="var(--danger)" strokeDasharray="4 4" label="max" />
                  <Area type="monotone" dataKey="connections" stroke="#3b82f6" fill="url(#connGrad)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Cache hit ratio & TPS">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={chartData}>
                  <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                  <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                  <YAxis yAxisId="left" stroke="#22d3ee" fontSize={11} domain={[0, 100]} />
                  <YAxis yAxisId="right" orientation="right" stroke="#22c55e" fontSize={11} />
                  <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="cacheHit" name="Cache hit %" stroke="#22d3ee" dot={false} strokeWidth={2} />
                  <Line yAxisId="right" type="monotone" dataKey="tps" name="TPS" stroke="#22c55e" dot={false} strokeWidth={2} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartCard>
          </div>

          {(events.length > 0 || predictions.length > 0) && (
            <div className="grid grid-2">
              {events.length > 0 && (
                <div className="card">
                  <h3 className="chart-title">Aktif alarmlar</h3>
                  <ul className="event-list">
                    {events.slice(0, 5).map((e) => (
                      <li key={e.id}>
                        <span className="event-dot" style={{ background: "var(--danger)" }} />
                        <span>{e.message}</span>
                        <span className="event-time">{formatTime(e.triggered_at)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {predictions.length > 0 && (
                <div className="card">
                  <h3 className="chart-title">Açık tahminler</h3>
                  <ul className="event-list">
                    {predictions.slice(0, 5).map((p) => (
                      <li key={p.id}>
                        <span className="event-dot" style={{ background: p.severity === "critical" ? "var(--danger)" : "var(--warning)" }} />
                        <span>{p.message}</span>
                        <span className="event-time">{p.predicted_value.toFixed(1)} / {p.threshold.toFixed(1)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {tab === "metrics" && (
        <div className="grid grid-2">
          <ChartCard title="Connections over time">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="connGrad2" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <ReferenceLine y={latest?.max_connections} stroke="var(--danger)" strokeDasharray="4 4" />
                <Area type="monotone" dataKey="connections" stroke="#3b82f6" fill="url(#connGrad2)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Cache hit ratio & TPS">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis yAxisId="left" stroke="#22d3ee" fontSize={11} domain={[0, 100]} />
                <YAxis yAxisId="right" orientation="right" stroke="#22c55e" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="cacheHit" name="Cache hit %" stroke="#22d3ee" dot={false} strokeWidth={2} />
                <Line yAxisId="right" type="monotone" dataKey="tps" name="TPS" stroke="#22c55e" dot={false} strokeWidth={2} />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Database size">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="sizeGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#f59e0b" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} tickFormatter={(v) => formatBytes(v)} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v) => formatBytes(Number(v))} />
                <Area type="monotone" dataKey="size" stroke="#f59e0b" fill="url(#sizeGrad)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Replication lag">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} tickFormatter={(v) => formatBytes(Number(v))} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v) => formatBytes(Number(v))} />
                <Line type="monotone" dataKey="lag" stroke="#f472b6" dot={false} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Deadlocks & temp bytes">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis yAxisId="left" stroke="#ef4444" fontSize={11} />
                <YAxis yAxisId="right" orientation="right" stroke="#a78bfa" fontSize={11} tickFormatter={(v) => formatBytes(Number(v))} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v, n) => [n === "temp" ? formatBytes(Number(v)) : v, n]} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="deadlocks" name="Deadlocks" stroke="#ef4444" dot={false} strokeWidth={2} />
                <Line yAxisId="right" type="monotone" dataKey="temp" name="Temp bytes" stroke="#a78bfa" dot={false} strokeWidth={2} />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="I/O blocks per second">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="ioReadGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#ef4444" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="ioHitGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#22c55e" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Area type="monotone" dataKey="blksRead" name="Disk read" stroke="#ef4444" fill="url(#ioReadGrad)" strokeWidth={2} />
                <Area type="monotone" dataKey="blksHit" name="Buffer hit" stroke="#22c55e" fill="url(#ioHitGrad)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Tuple throughput">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Line type="monotone" dataKey="tupReturned" name="Returned" stroke="#3b82f6" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupFetched" name="Fetched" stroke="#22d3ee" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupInserted" name="Inserted" stroke="#22c55e" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupUpdated" name="Updated" stroke="#f59e0b" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupDeleted" name="Deleted" stroke="#ef4444" dot={false} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Temp files & bytes">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis yAxisId="left" stroke="#f59e0b" fontSize={11} />
                <YAxis yAxisId="right" orientation="right" stroke="#a78bfa" fontSize={11} tickFormatter={(v) => formatBytes(Number(v))} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v, n) => [n === "tempBytes" ? formatBytes(Number(v)) : v, n]} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="tempFiles" name="Temp files / s" stroke="#f59e0b" dot={false} strokeWidth={2} />
                <Line yAxisId="right" type="monotone" dataKey="tempBytes" name="Temp bytes / s" stroke="#a78bfa" dot={false} strokeWidth={2} />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Checkpoints & buffers">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Line type="monotone" dataKey="checkpointsTimed" name="Timed checkpoints" stroke="#3b82f6" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="checkpointsReq" name="Requested checkpoints" stroke="#ef4444" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="buffersCheckpoint" name="Buffers checkpoint / s" stroke="#22c55e" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="buffersBackend" name="Buffers backend / s" stroke="#f59e0b" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="buffersClean" name="Buffers clean / s" stroke="#a78bfa" dot={false} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>
        </div>
      )}

      {tab === "queries" && (
        <>
          <div className="card">
            <div className="queries-header">
              <h3 className="chart-title">Slow query dağılımı (top 10)</h3>
              <div className="sort-bar">
                <span>Sırala:</span>
                {(["total", "mean", "calls"] as const).map((k) => (
                  <button key={k} className={`sort-btn${querySort === k ? " active" : ""}`} onClick={() => setQuerySort(k)}>
                    {k === "total" ? "Toplam süre" : k === "mean" ? "Ortalama süre" : "Çağrı sayısı"}
                  </button>
                ))}
              </div>
            </div>
            {topQueriesChart.length === 0 ? (
              <div className="empty">Yavaş sorgu verisi yok</div>
            ) : (
              <div style={{ height: 280 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={topQueriesChart} layout="vertical" margin={{ left: 20, right: 20 }}>
                    <CartesianGrid stroke="#243049" strokeDasharray="3 3" horizontal={false} />
                    <XAxis type="number" stroke="#8b9bb8" fontSize={11} />
                    <YAxis dataKey="name" type="category" stroke="#8b9bb8" fontSize={11} width={40} />
                    <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                    <Bar dataKey="total" fill="var(--accent)" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          <div className="card">
            <h3 className="chart-title">Slow query detayları ({queries.length})</h3>
            {queries.length === 0 ? (
              <div className="empty">
                No slow query data. Enable the extension: <code>CREATE EXTENSION pg_stat_statements;</code>
              </div>
            ) : (
              <div className="table-wrap">
                <table className="query-table">
                  <thead>
                    <tr>
                      <th>Query</th>
                      <th>Calls</th>
                      <th>Mean (ms)</th>
                      <th>Total (ms)</th>
                      <th>Rows</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedQueries.map((q) => (
                      <Fragment key={q.id}>
                        <tr className="query-row" onClick={() => setExpandedQuery(expandedQuery === q.id ? null : q.id)}>
                          <td className="query-cell">{queryFingerprint(q.query)}</td>
                          <td>{q.calls.toLocaleString()}</td>
                          <td>{q.mean_time_ms.toFixed(2)}</td>
                          <td>{q.total_time_ms.toFixed(1)}</td>
                          <td>{q.rows.toLocaleString()}</td>
                        </tr>
                        {expandedQuery === q.id && (
                          <tr className="query-expanded">
                            <td colSpan={5}>
                              <pre>{q.query}</pre>
                              <p>queryid: {q.queryid || "—"}</p>
                              <div className="query-stats">
                            <h4>Sorgu düzeyinde I/O & CPU</h4>
                            <div className="query-stats-grid">
                              <div><span>shared read</span><strong>{q.shared_blks_read ?? 0}</strong></div>
                              <div><span>shared hit</span><strong>{q.shared_blks_hit ?? 0}</strong></div>
                              <div><span>local read</span><strong>{q.local_blks_read ?? 0}</strong></div>
                              <div><span>local hit</span><strong>{q.local_blks_hit ?? 0}</strong></div>
                              <div><span>temp read</span><strong>{q.temp_blks_read ?? 0}</strong></div>
                              <div><span>temp write</span><strong>{q.temp_blks_written ?? 0}</strong></div>
                              {(q.exec_user_time || q.exec_sys_time) && (
                                <div><span>CPU (exec)</span><strong>{((q.exec_user_time ?? 0) + (q.exec_sys_time ?? 0)).toFixed(2)} ms</strong></div>
                              )}
                              {(q.plan_user_time || q.plan_sys_time) && (
                                <div><span>CPU (plan)</span><strong>{((q.plan_user_time ?? 0) + (q.plan_sys_time ?? 0)).toFixed(2)} ms</strong></div>
                              )}
                            </div>
                          </div>
                          <div className="advice-section">
                                <button
                                  className="btn btn-primary"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    loadAdvice(q);
                                  }}
                                  disabled={adviceLoading[q.id]}
                                >
                                  {adviceLoading[q.id] ? "Analyzing…" : "Index önerisi al"}
                                </button>
                                {advice[q.id] && (
                                  <div className="advice-results">
                                    {advice[q.id].length === 0 ? (
                                      <p className="advice-empty">Açık index önerisi bulunamadı.</p>
                                    ) : (
                                      advice[q.id].map((a) => (
                                        <div className="advice-card" key={a.index_ddl}>
                                          <div className="advice-header">
                                            <span className="advice-table">{a.schema_name}.{a.table_name}</span>
                                            <span className="advice-pill">
                                              Tahmini iyileştirme: <strong>%{a.estimated_improvement_pct}</strong>
                                              {a.has_hypopg_estimate && " (hypopg gerçek plan)"}
                                            </span>
                                          </div>
                                          <p className="advice-reason">{a.reason}</p>
                                          <code className="advice-ddl">{a.index_ddl}</code>
                                          {a.before_cost !== null && a.after_cost !== null && (
                                            <div className="advice-costs">
                                              <span>Plan cost: {a.before_cost.toFixed(1)} → {a.after_cost.toFixed(1)}</span>
                                            </div>
                                          )}
                                        </div>
                                      ))
                                    )}
                                  </div>
                                )}
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {latest && (
              <p style={{ color: "var(--muted)", fontSize: "0.8rem", marginTop: "0.75rem" }}>
                Last collected: {formatTime(latest.collected_at)}
              </p>
            )}
          </div>
        </>
      )}

      {tab === "alerts" && (
        <div className="grid grid-2">
          <div className="card">
            <h3 className="chart-title">Alert kuralları</h3>
            {rules.length === 0 ? (
              <div className="empty">Kural tanımlı değil</div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Kural</th><th>Metrik</th><th>Operatör</th><th>Eşik</th><th>Durum</th></tr>
                  </thead>
                  <tbody>
                    {rules.map((r) => (
                      <tr key={r.id}>
                        <td>{r.name}</td>
                        <td>{r.metric}</td>
                        <td>{r.operator}</td>
                        <td>{r.threshold}</td>
                        <td>{r.enabled ? "Aktif" : "Pasif"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          <div className="card">
            <h3 className="chart-title">Son alert olayları</h3>
            {events.length === 0 ? (
              <div className="empty">Aktif olay yok</div>
            ) : (
              <ul className="event-list">
                {events.map((e) => (
                  <li key={e.id}>
                    <span className="event-dot" style={{ background: e.resolved_at ? "var(--success)" : "var(--danger)" }} />
                    <span>{e.message}</span>
                    <span className="event-time">{formatTime(e.triggered_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {tab === "predictions" && (
        <div className="card">
          <h3 className="chart-title">Tahminler</h3>
          {predictions.length === 0 ? (
            <div className="empty">Açık tahmin yok</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Metrik</th><th>Güncel</th><th>Tahmin</th><th>Eşik</th><th>Ciddiyet</th><th>Mesaj</th></tr>
                </thead>
                <tbody>
                  {predictions.map((p) => (
                    <tr key={p.id}>
                      <td>{p.metric_key}</td>
                      <td>{p.current_value.toFixed(2)}</td>
                      <td>{p.predicted_value.toFixed(2)}</td>
                      <td>{p.threshold.toFixed(2)}</td>
                      <td><span className={`status ${p.severity}`}>{p.severity}</span></td>
                      <td>{p.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </>
  );
}
