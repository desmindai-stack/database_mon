import type { ActivitySnapshot } from "../api";

type Props = {
  activity: ActivitySnapshot | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
};

function fmtSec(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

export default function ActivityPanel({ activity, error, loading, onRefresh }: Props) {
  if (error) {
    return (
      <div className="card">
        <div className="error">{error}</div>
        <button className="btn" onClick={onRefresh}>Yeniden dene</button>
      </div>
    );
  }

  if (!activity) {
    return <div className="card empty">{loading ? "Activity yükleniyor…" : "Activity verisi yok"}</div>;
  }

  const { totals, sessions, wait_events, blocking, state_summary } = activity;

  return (
    <div className="activity-layout">
      <div className="activity-toolbar">
        <div>
          <h3 className="chart-title" style={{ margin: 0 }}>Canlı oturumlar</h3>
          <p className="muted-note">pg_stat_activity · blocking · wait events</p>
        </div>
        <button className="btn" onClick={onRefresh} disabled={loading}>
          {loading ? "Yenileniyor…" : "Yenile"}
        </button>
      </div>

      <div className="stats-grid compact">
        <div className="card stat-tile"><div className="stat-tile-label">Toplam</div><div className="stat-tile-value">{totals.total}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Active</div><div className="stat-tile-value">{totals.active}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Idle</div><div className="stat-tile-value">{totals.idle}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Idle in tx</div><div className={`stat-tile-value${totals.idle_in_transaction ? " warn-text" : ""}`}>{totals.idle_in_transaction}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Waiting</div><div className="stat-tile-value">{totals.waiting}</div></div>
        <div className="card stat-tile"><div className="stat-tile-label">Blocked</div><div className={`stat-tile-value${totals.blocked ? " danger-text" : ""}`}>{totals.blocked}</div></div>
      </div>

      {blocking.length > 0 && (
        <div className="card blocking-card">
          <h3 className="chart-title">Blocking zinciri</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Blocked PID</th>
                  <th>Blocking PID</th>
                  <th>Wait</th>
                  <th>Süre</th>
                  <th>Sorgu</th>
                </tr>
              </thead>
              <tbody>
                {blocking.map((b, idx) => (
                  <tr key={`${b.blocked_pid}-${b.blocking_pid}-${idx}`}>
                    <td>{b.blocked_pid}</td>
                    <td className="danger-text">{b.blocking_pid}</td>
                    <td>{b.wait_event_type || "—"} / {b.wait_event || "—"}</td>
                    <td>{fmtSec(b.duration_sec)}</td>
                    <td className="query-cell">{b.blocked_query.slice(0, 160)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="grid grid-2">
        <div className="card">
          <h3 className="chart-title">Wait event dağılımı</h3>
          {wait_events.length === 0 ? (
            <div className="empty">Aktif wait event yok</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Type</th><th>Event</th><th>Adet</th></tr>
                </thead>
                <tbody>
                  {wait_events.map((w) => (
                    <tr key={`${w.wait_event_type}-${w.wait_event}`}>
                      <td>{w.wait_event_type}</td>
                      <td>{w.wait_event}</td>
                      <td>{w.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="card">
          <h3 className="chart-title">State özeti</h3>
          {state_summary.length === 0 ? (
            <div className="empty">State yok</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>State</th><th>Adet</th></tr>
                </thead>
                <tbody>
                  {state_summary.map((s) => (
                    <tr key={s.state}>
                      <td>{s.state}</td>
                      <td>{s.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <h3 className="chart-title">Oturum listesi</h3>
        {sessions.length === 0 ? (
          <div className="empty">Client backend oturumu yok</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>PID</th>
                  <th>User</th>
                  <th>App</th>
                  <th>State</th>
                  <th>Wait</th>
                  <th>Süre</th>
                  <th>Sorgu</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.pid} className={s.blocked ? "row-blocked" : s.state === "active" ? "row-active" : ""}>
                    <td>{s.pid}</td>
                    <td>{s.usename || "—"}</td>
                    <td>{s.application_name || "—"}</td>
                    <td>
                      <span className={`state-pill ${s.state.replace(/\s+/g, "-")}`}>{s.state}</span>
                      {s.blocked && <span className="state-pill blocked">blocked</span>}
                    </td>
                    <td>{s.wait_event_type ? `${s.wait_event_type}/${s.wait_event}` : "—"}</td>
                    <td>{fmtSec(s.query_duration_sec)}</td>
                    <td className="query-cell" title={s.query}>{s.query.slice(0, 140) || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
