import { useState } from "react";
import type { ClusterHealth, ClusterLogs } from "../api";
import { api } from "../api";

type Props = {
  instanceId: number;
  data: ClusterHealth | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
};

const STATUS_TR: Record<string, string> = {
  up: "UP",
  down: "DOWN",
  unknown: "UNKNOWN",
  skipped: "SKIP",
};

export default function ClusterHealthPanel({ instanceId, data, error, loading, onRefresh }: Props) {
  const [logService, setLogService] = useState("patroni");
  const [logs, setLogs] = useState<ClusterLogs | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  const loadLogs = async (service: string) => {
    setLogService(service);
    setLogLoading(true);
    setLogError(null);
    try {
      const result = await api.getClusterLogs(instanceId, service, 120);
      setLogs(result);
    } catch (e) {
      setLogs(null);
      setLogError(String((e as Error).message || e));
    } finally {
      setLogLoading(false);
    }
  };

  if (error) {
    return (
      <div className="card">
        <div className="error">{error}</div>
        <button className="btn" onClick={onRefresh}>Yeniden dene</button>
      </div>
    );
  }

  if (!data) {
    return <div className="card empty">{loading ? "Cluster health yükleniyor…" : "Veri yok"}</div>;
  }

  return (
    <div className="cluster-layout">
      <div className="activity-toolbar">
        <div>
          <h3 className="chart-title" style={{ margin: 0 }}>
            Cluster health
            <span className={`tuning-status ${data.overall}`} style={{ marginLeft: "0.6rem" }}>
              {data.overall}
            </span>
          </h3>
          <p className="muted-note">
            {data.cluster_name || "Cluster adı yok"} · {new Date(data.checked_at).toLocaleString()}
            {data.agent.configured
              ? ` · agent ${data.agent.reachable ? "online" : "unreachable"}`
              : " · agent yok (probe-only)"}
          </p>
        </div>
        <button className="btn" onClick={onRefresh} disabled={loading}>
          {loading ? "Yenileniyor…" : "Yenile"}
        </button>
      </div>

      <div className="stats-grid compact">
        <div className="card stat-tile"><div className="stat-tile-label">UP</div><div className="stat-tile-value">{data.totals.up}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">DOWN</div><div className={`stat-tile-value${data.totals.down ? " danger-text" : ""}`}>{data.totals.down}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Unknown</div><div className="stat-tile-value">{data.totals.unknown}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Skipped</div><div className="stat-tile-value">{data.totals.skipped}</div></div>
      </div>

      {data.cluster && (
        <div className="card">
          <h3 className="chart-title">Patroni cluster</h3>
          <p className="muted-note">
            Leader: <strong>{data.cluster.leader || "yok"}</strong> · Members: {data.cluster.member_count}
          </p>
          {data.cluster.members.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Node</th><th>Role</th><th>State</th><th>Host</th></tr>
                </thead>
                <tbody>
                  {data.cluster.members.map((m) => (
                    <tr key={`${m.name}-${m.host}`}>
                      <td>{m.name || "—"}</td>
                      <td>{m.role || "—"}</td>
                      <td>{m.state || "—"}</td>
                      <td>{m.host || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="card">
        <h3 className="chart-title">Servis durumu</h3>
        <div className="cluster-service-grid">
          {data.services.map((svc) => (
            <div key={svc.service} className={`cluster-service-card status-${svc.status}`}>
              <div className="cluster-service-head">
                <strong>{svc.service}</strong>
                <span className={`state-pill ${svc.status}`}>{STATUS_TR[svc.status] || svc.status}</span>
              </div>
              <p className="muted-note">{svc.detail || "—"}</p>
              <div className="cluster-service-meta">
                {svc.latency_ms != null && <span>{svc.latency_ms.toFixed(0)} ms</span>}
                <span>{svc.source}</span>
                {svc.role && <span>role={svc.role}</span>}
                {svc.systemd_active && <span>systemd={svc.systemd_active}</span>}
                {svc.up_backends != null && <span>UP backends={svc.up_backends}</span>}
              </div>
              {data.agent.reachable && svc.status !== "skipped" && (
                <button className="btn linkish" onClick={() => loadLogs(svc.service)}>
                  Son loglar →
                </button>
              )}
            </div>
          ))}
        </div>
      </div>

      {(data.agent.reachable || logs || logError) && (
        <div className="card">
          <div className="activity-toolbar">
            <h3 className="chart-title" style={{ margin: 0 }}>Servis logları ({logService})</h3>
            <div className="detail-actions">
              {["etcd", "patroni", "postgresql", "keepalived", "haproxy"].map((svc) => (
                <button key={svc} className="btn" onClick={() => loadLogs(svc)} disabled={logLoading}>
                  {svc}
                </button>
              ))}
            </div>
          </div>
          {logLoading && <p className="muted-note">Log yükleniyor…</p>}
          {logError && <div className="error">{logError}</div>}
          {logs && (
            <>
              {logs.unit && <p className="muted-note">unit: {logs.unit}</p>}
              {logs.error && <div className="error">{logs.error}</div>}
              <pre className="cluster-log-view">{(logs.lines || []).join("\n") || "(boş)"}</pre>
            </>
          )}
          {!logs && !logLoading && !logError && (
            <p className="muted-note">Bir servise tıklayarak journalctl çıktısını çekin.</p>
          )}
        </div>
      )}
    </div>
  );
}
