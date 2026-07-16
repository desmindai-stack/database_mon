import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatBytes, InstanceSummary } from "../api";

export default function DashboardPage() {
  const [summaries, setSummaries] = useState<InstanceSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getSummaries()
      .then(setSummaries)
      .catch((err) => setError(String(err.message || err)));
  }, []);

  const totalConnections = summaries.reduce(
    (sum, s) => sum + (s.latest_metrics?.active_connections ?? 0),
    0,
  );
  const alerting = summaries.filter((s) => s.status === "alerting").length;

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Dashboard</h2>
          <p>Overview of all monitored PostgreSQL instances</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-4" style={{ marginBottom: "1rem" }}>
        <div className="card">
          <h3>Instances</h3>
          <div className="value">{summaries.length}</div>
        </div>
        <div className="card">
          <h3>Active connections</h3>
          <div className="value">{totalConnections}</div>
        </div>
        <div className="card">
          <h3>Alerting</h3>
          <div className="value">{alerting}</div>
        </div>
        <div className="card">
          <h3>Healthy</h3>
          <div className="value">{summaries.filter((s) => s.status === "healthy").length}</div>
        </div>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Instance</th>
              <th>Status</th>
              <th>Connections</th>
              <th>Cache hit</th>
              <th>TPS</th>
              <th>DB size</th>
              <th>Alerts</th>
            </tr>
          </thead>
          <tbody>
            {summaries.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty">
                  No instances yet. <Link to="/instances">Add your first PostgreSQL server</Link>.
                </td>
              </tr>
            ) : (
              summaries.map(({ instance, latest_metrics, status, alerts_firing }) => (
                <tr key={instance.id}>
                  <td>
                    <Link to={`/instances/${instance.id}`}>{instance.name}</Link>
                    <div style={{ color: "var(--muted)", fontSize: "0.8rem" }}>
                      {instance.host}:{instance.port}/{instance.database}
                    </div>
                  </td>
                  <td><span className={`status ${status}`}>{status}</span></td>
                  <td>
                    {latest_metrics
                      ? `${latest_metrics.active_connections} / ${latest_metrics.max_connections}`
                      : "—"}
                  </td>
                  <td>{latest_metrics ? `${latest_metrics.cache_hit_ratio.toFixed(1)}%` : "—"}</td>
                  <td>{latest_metrics ? latest_metrics.transactions_per_sec.toFixed(1) : "—"}</td>
                  <td>{latest_metrics ? formatBytes(latest_metrics.database_size_bytes) : "—"}</td>
                  <td>{alerts_firing || "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
