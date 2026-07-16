import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, formatBytes, formatTime, MetricSample, SlowQuery } from "../api";

export default function InstanceDetailPage() {
  const { id } = useParams();
  const instanceId = Number(id);
  const [metrics, setMetrics] = useState<MetricSample[]>([]);
  const [queries, setQueries] = useState<SlowQuery[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!instanceId) return;
    Promise.all([api.getMetrics(instanceId, 2), api.getSlowQueries(instanceId)])
      .then(([m, q]) => {
        setMetrics(m);
        setQueries(q);
      })
      .catch((e) => setError(String(e.message || e)));
    const timer = setInterval(() => {
      api.getMetrics(instanceId, 2).then(setMetrics).catch(() => undefined);
      api.getSlowQueries(instanceId).then(setQueries).catch(() => undefined);
    }, 15000);
    return () => clearInterval(timer);
  }, [instanceId]);

  const latest = metrics.at(-1);
  const chartData = useMemo(
    () =>
      metrics.map((m) => ({
        time: new Date(m.collected_at).toLocaleTimeString(),
        connections: m.active_connections,
        cacheHit: m.cache_hit_ratio,
        tps: m.transactions_per_sec,
      })),
    [metrics],
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Instance #{instanceId}</h2>
          <p><Link to="/instances">← Back to instances</Link></p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      {latest && (
        <div className="grid grid-4" style={{ marginBottom: "1rem" }}>
          <div className="card"><h3>Connections</h3><div className="value">{latest.active_connections}/{latest.max_connections}</div></div>
          <div className="card"><h3>Cache hit ratio</h3><div className="value">{latest.cache_hit_ratio.toFixed(1)}%</div></div>
          <div className="card"><h3>Transactions/sec</h3><div className="value">{latest.transactions_per_sec.toFixed(1)}</div></div>
          <div className="card"><h3>Database size</h3><div className="value">{formatBytes(latest.database_size_bytes)}</div></div>
        </div>
      )}

      <div className="grid grid-2" style={{ marginBottom: "1rem" }}>
        <div className="card" style={{ height: 320 }}>
          <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>Connections over time</h3>
          <ResponsiveContainer width="100%" height="85%">
            <LineChart data={chartData}>
              <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
              <XAxis dataKey="time" stroke="#8b9bb8" fontSize={12} />
              <YAxis stroke="#8b9bb8" fontSize={12} />
              <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
              <Line type="monotone" dataKey="connections" stroke="#3b82f6" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="card" style={{ height: 320 }}>
          <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>Cache hit & TPS</h3>
          <ResponsiveContainer width="100%" height="85%">
            <LineChart data={chartData}>
              <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
              <XAxis dataKey="time" stroke="#8b9bb8" fontSize={12} />
              <YAxis stroke="#8b9bb8" fontSize={12} />
              <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
              <Line type="monotone" dataKey="cacheHit" stroke="#22d3ee" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="tps" stroke="#22c55e" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card">
        <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>Slow queries (pg_stat_statements)</h3>
        {queries.length === 0 ? (
          <div className="empty">
            No slow query data. Enable the extension: <code>CREATE EXTENSION pg_stat_statements;</code>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Mean time (ms)</th>
                  <th>Calls</th>
                  <th>Total time (ms)</th>
                  <th>Query</th>
                </tr>
              </thead>
              <tbody>
                {queries.map((q) => (
                  <tr key={q.id}>
                    <td>{q.mean_time_ms.toFixed(2)}</td>
                    <td>{q.calls}</td>
                    <td>{q.total_time_ms.toFixed(1)}</td>
                    <td className="query-cell">{q.query}</td>
                  </tr>
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
  );
}
