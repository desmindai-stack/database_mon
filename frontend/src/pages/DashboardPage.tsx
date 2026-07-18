import { useEffect, useMemo, useState } from "react";
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
  const predictions = summaries.reduce((sum, s) => sum + (s.predictions_open ?? 0), 0);

  const grouped = useMemo(() => {
    const map = new Map<string, InstanceSummary[]>();
    for (const s of summaries) {
      const key = `${s.instance.customer_name || "Bilinmeyen Müşteri"} / ${s.instance.application || "—"}`;
      const list = map.get(key) ?? [];
      list.push(s);
      map.set(key, list);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [summaries]);

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Dashboard</h2>
          <p>Tüm veritabanı instance’larının DBA özeti</p>
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
          <h3>Tahmin (açık)</h3>
          <div className="value">{predictions}</div>
        </div>
        <div className="card">
          <h3>Healthy</h3>
          <div className="value">{summaries.filter((s) => s.status === "healthy").length}</div>
        </div>
      </div>

      {grouped.map(([group, items]) => (
        <div className="card" key={group} style={{ marginBottom: "1rem" }}>
          <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>{group}</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Instance</th>
                  <th>Ortam</th>
                  <th>Cluster</th>
                  <th>Rol</th>
                  <th>Motor</th>
                  <th>Durum</th>
                  <th>Connections</th>
                  <th>Cache hit</th>
                  <th>TPS</th>
                  <th>DB size</th>
                  <th>Alarm</th>
                </tr>
              </thead>
              <tbody>
                {items.map(({ instance, latest_metrics, status, alerts_firing }) => (
                  <tr key={instance.id}>
                    <td>
                      <Link to={`/instances/${instance.id}`}>{instance.name}</Link>
                      <div style={{ color: "var(--muted)", fontSize: "0.8rem" }}>
                        {instance.host}:{instance.port}/{instance.database}
                      </div>
                    </td>
                    <td>{instance.environment}</td>
                    <td>{instance.cluster_name || "—"}</td>
                    <td>{instance.role || "—"}</td>
                    <td>{instance.engine}</td>
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
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </>
  );
}
