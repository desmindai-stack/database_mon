import { formatBytes } from "../api";
import type { SchemaHealth } from "../api";

type Props = {
  data: SchemaHealth | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
};

function fmtLag(sec: number): string {
  if (!sec) return "—";
  if (sec < 3600) return `${(sec / 60).toFixed(0)} dk`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} sa`;
  return `${(sec / 86400).toFixed(1)} gün`;
}

export default function SchemaHealthPanel({ data, error, loading, onRefresh }: Props) {
  if (error) {
    return (
      <div className="card">
        <div className="error">{error}</div>
        <button className="btn" onClick={onRefresh}>Yeniden dene</button>
      </div>
    );
  }

  if (!data) {
    return <div className="card empty">{loading ? "Schema health yükleniyor…" : "Veri yok"}</div>;
  }

  const { totals, unused_indexes, bloated_tables, vacuum_lag } = data;

  return (
    <div className="schema-layout">
      <div className="activity-toolbar">
        <div>
          <h3 className="chart-title" style={{ margin: 0 }}>Schema health</h3>
          <p className="muted-note">Unused index · dead tuple / bloat · vacuum lag</p>
        </div>
        <button className="btn" onClick={onRefresh} disabled={loading}>
          {loading ? "Yenileniyor…" : "Yenile"}
        </button>
      </div>

      <div className="stats-grid compact">
        <div className="card stat-tile">
          <div className="stat-tile-label">Unused index</div>
          <div className={`stat-tile-value${totals.unused_indexes ? " warn-text" : ""}`}>{totals.unused_indexes}</div>
          <div className="stat-tile-sub">{formatBytes(totals.unused_index_bytes)}</div>
        </div>
        <div className="card stat-tile">
          <div className="stat-tile-label">Bloat riski</div>
          <div className={`stat-tile-value${totals.bloated_tables ? " warn-text" : ""}`}>{totals.bloated_tables}</div>
        </div>
        <div className="card stat-tile">
          <div className="stat-tile-label">Vacuum lag</div>
          <div className={`stat-tile-value${totals.vacuum_lag_tables ? " danger-text" : ""}`}>{totals.vacuum_lag_tables}</div>
        </div>
      </div>

      <div className="card">
        <h3 className="chart-title">Kullanılmayan indexler</h3>
        {unused_indexes.length === 0 ? (
          <div className="empty">Unused index bulunamadı (veya hepsi unique/PK)</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tablo</th>
                  <th>Index</th>
                  <th>Boyut</th>
                  <th>Scan</th>
                  <th>DROP önerisi</th>
                </tr>
              </thead>
              <tbody>
                {unused_indexes.map((idx) => (
                  <tr key={`${idx.schema_name}.${idx.index_name}`}>
                    <td>{idx.schema_name}.{idx.table_name}</td>
                    <td>{idx.index_name}</td>
                    <td>{formatBytes(idx.index_bytes)}</td>
                    <td>{idx.idx_scan}</td>
                    <td>
                      <code className="ddl-code">{idx.drop_ddl}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h3 className="chart-title">Dead tuple / bloat riski</h3>
        {bloated_tables.length === 0 ? (
          <div className="empty">Belirgin bloat riski yok</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tablo</th>
                  <th>Live</th>
                  <th>Dead</th>
                  <th>Dead %</th>
                  <th>Boyut</th>
                  <th>Last autovacuum</th>
                  <th>Severity</th>
                </tr>
              </thead>
              <tbody>
                {bloated_tables.map((t) => (
                  <tr key={`${t.schema_name}.${t.table_name}`}>
                    <td>{t.schema_name}.{t.table_name}</td>
                    <td>{t.live_tup.toLocaleString()}</td>
                    <td>{t.dead_tup.toLocaleString()}</td>
                    <td>{t.dead_ratio_pct.toFixed(1)}%</td>
                    <td>{formatBytes(t.table_bytes)}</td>
                    <td>{t.last_autovacuum ? new Date(t.last_autovacuum).toLocaleString() : "hiç"}</td>
                    <td><span className={`insight-severity ${t.severity}`}>{t.severity}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h3 className="chart-title">Vacuum / analyze lag</h3>
        {vacuum_lag.length === 0 ? (
          <div className="empty">Vacuum lag sorunu yok</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tablo</th>
                  <th>Lag</th>
                  <th>Freeze age</th>
                  <th>Dead</th>
                  <th>Severity</th>
                </tr>
              </thead>
              <tbody>
                {vacuum_lag.map((t) => (
                  <tr key={`${t.schema_name}.${t.table_name}-lag`}>
                    <td>{t.schema_name}.{t.table_name}</td>
                    <td>{t.last_autovacuum ? fmtLag(t.lag_sec) : "hiç vacuum yok"}</td>
                    <td>{t.freeze_age.toLocaleString()}</td>
                    <td>{t.dead_tup.toLocaleString()}</td>
                    <td><span className={`insight-severity ${t.severity}`}>{t.severity}</span></td>
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
