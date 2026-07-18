import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatBytes, HealthResponse, InstanceSummary } from "../api";

export default function DashboardPage() {
  const [summaries, setSummaries] = useState<InstanceSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<HealthResponse | null>(null);

  useEffect(() => {
    api.getSummaries()
      .then(setSummaries)
      .catch((err) => setError(String(err.message || err)));
    api.getHealth().then(setConfig).catch(() => undefined);
  }, []);

  const totalConnections = summaries.reduce(
    (sum, s) => sum + (s.latest_metrics?.active_connections ?? 0),
    0,
  );
  const alerting = summaries.filter((s) => s.status === "alerting").length;
  const warnings = summaries.filter((s) => s.status === "warning").length;
  const predictions = summaries.reduce((sum, s) => sum + (s.predictions_open ?? 0), 0);
  const healthy = summaries.filter((s) => s.status === "healthy").length;
  const pending = summaries.filter((s) => s.status === "pending").length;

  const isPrivate = config?.deployment_mode === "private";

  const grouped = useMemo(() => {
    const map = new Map<string, InstanceSummary[]>();
    for (const s of summaries) {
      const key = isPrivate
        ? `${s.instance.application || "Uygulama"}${s.instance.cluster_name ? " / " + s.instance.cluster_name : ""}`
        : `${s.instance.customer_name || "Bilinmeyen Müşteri"} / ${s.instance.application || "—"}`;
      const list = map.get(key) ?? [];
      list.push(s);
      map.set(key, list);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [summaries, isPrivate]);

  const StatCard = ({ label, value, color }: { label: string; value: string | number; color?: string }) => (
    <div className="card" style={{ borderLeft: `4px solid ${color || "var(--accent)"}` }}>
      <h3>{label}</h3>
      <div className="value">{value}</div>
    </div>
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Dashboard</h2>
          <p>
            {isPrivate
              ? `${config?.default_customer_name || "Private"} ortamı DBA özeti`
              : "Tüm müşteri veritabanı instance’larının DBA özeti"}
          </p>
        </div>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <Link to="/instances" className="btn">Tüm instance’lar</Link>
          <Link to="/instances" className="btn btn-primary">+ Yeni instance</Link>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-4" style={{ marginBottom: "1rem" }}>
        <StatCard label="Toplam instance" value={summaries.length} color="var(--accent)" />
        <StatCard label="Active connections" value={totalConnections} color="#22d3ee" />
        <StatCard label="Healthy" value={healthy} color="var(--success)" />
        <StatCard label="Alerting" value={alerting} color="var(--danger)" />
        <StatCard label="Warning" value={warnings} color="var(--warning)" />
        <StatCard label="Tahmin (açık)" value={predictions} color="#a78bfa" />
        <StatCard label="Pending" value={pending} color="var(--muted)" />
      </div>

      {grouped.length === 0 ? (
        <div className="card" style={{ padding: "2rem", textAlign: "center" }}>
          <p style={{ color: "var(--muted)" }}>
            Henüz instance yok. <Link to="/instances">İlk veritabanınızı ekleyin</Link>.
          </p>
        </div>
      ) : (
        grouped.map(([group, items]) => (
          <div className="card" key={group} style={{ marginBottom: "1rem" }}>
            <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>{group}</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Instance</th>
                    {!isPrivate && <th>Müşteri</th>}
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
                      {!isPrivate && <td>{instance.customer_name || "—"}</td>}
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
        ))
      )}
    </>
  );
}
