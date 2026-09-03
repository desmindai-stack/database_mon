import { useState } from "react";
import { formatBytes } from "../api";
import type { SchemaHealth } from "../api";
import CopyableAction from "./CopyableAction";

type Props = {
  data: SchemaHealth | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
};

/** Backend'in ürettiği üç seviye ↔ kullanıcıya gösterilen üç kova (Faz 16-B İŞ 5). */
type Severity = "critical" | "high" | "medium";
const SEVERITY_LABELS: Record<Severity, string> = { critical: "Kritik", high: "Uyarı", medium: "Bilgi" };
const ALL_SEVERITIES: Severity[] = ["critical", "high", "medium"];

function fmtLag(sec: number): string {
  if (!sec) return "—";
  if (sec < 3600) return `${(sec / 60).toFixed(0)} dk`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} sa`;
  return `${(sec / 86400).toFixed(1)} gün`;
}

export default function SchemaHealthPanel({ data, error, loading, onRefresh }: Props) {
  // Varsayılan: hepsi açık — filtre bir daraltma aracı, veriyi gizleyerek başlamamalı.
  const [selected, setSelected] = useState<Set<Severity>>(new Set(ALL_SEVERITIES));

  const toggle = (s: Severity) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });

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

  const { totals } = data;
  const keep = <T extends { severity: string }>(rows: T[]) =>
    rows.filter((r) => selected.has((r.severity as Severity) ?? "medium"));

  const unused_indexes = keep(data.unused_indexes);
  const bloated_tables = keep(data.bloated_tables);
  const vacuum_lag = keep(data.vacuum_lag);
  const hiddenCount =
    data.unused_indexes.length -
    unused_indexes.length +
    (data.bloated_tables.length - bloated_tables.length) +
    (data.vacuum_lag.length - vacuum_lag.length);

  const emptyText = (allRows: unknown[], base: string) =>
    allRows.length > 0 ? "Seçili önem derecelerinde kayıt yok (filtreyi genişletin)" : base;

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

      {/* Faz 16-B İŞ 5: önem derecesine göre çoklu seçim filtresi — üç listeye birden uygulanır. */}
      <div className="severity-filter">
        <span>Önem derecesi:</span>
        {ALL_SEVERITIES.map((s) => (
          <label key={s} className={`severity-chip ${s}${selected.has(s) ? " active" : ""}`}>
            <input type="checkbox" checked={selected.has(s)} onChange={() => toggle(s)} />
            {SEVERITY_LABELS[s]}
          </label>
        ))}
        {hiddenCount > 0 && <span className="muted-note">{hiddenCount} kayıt filtrelendi</span>}
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
          <div className="empty">{emptyText(data.unused_indexes, "Unused index bulunamadı (veya hepsi unique/PK)")}</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tablo</th>
                  <th>Index</th>
                  <th>Boyut</th>
                  <th>Scan</th>
                  <th>Önem</th>
                  <th>DROP komutu</th>
                </tr>
              </thead>
              <tbody>
                {unused_indexes.map((idx) => (
                  <tr key={`${idx.schema_name}.${idx.index_name}`}>
                    <td>{idx.schema_name}.{idx.table_name}</td>
                    <td>{idx.index_name}</td>
                    <td>{formatBytes(idx.index_bytes)}</td>
                    <td>{idx.idx_scan}</td>
                    <td><span className={`insight-severity ${idx.severity}`}>{SEVERITY_LABELS[idx.severity as Severity]}</span></td>
                    {/* Komut daha önce tek satırlık <code> içinde CSS ile kırpılıyordu ve
                        yarım görünüyordu; artık tam metin + kopyala butonu (Faz 16-B İŞ 5). */}
                    <td className="ddl-cell"><CopyableAction command={idx.drop_ddl} /></td>
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
          <div className="empty">{emptyText(data.bloated_tables, "Belirgin bloat riski yok")}</div>
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
                  <th>Önem</th>
                  <th>Komut</th>
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
                    <td><span className={`insight-severity ${t.severity}`}>{SEVERITY_LABELS[t.severity as Severity]}</span></td>
                    <td className="ddl-cell">{t.vacuum_ddl && <CopyableAction command={t.vacuum_ddl} />}</td>
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
          <div className="empty">{emptyText(data.vacuum_lag, "Vacuum lag sorunu yok")}</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tablo</th>
                  <th>Lag</th>
                  <th>Freeze age</th>
                  <th>Dead</th>
                  <th>Önem</th>
                  <th>Komut</th>
                </tr>
              </thead>
              <tbody>
                {vacuum_lag.map((t) => (
                  <tr key={`${t.schema_name}.${t.table_name}-lag`}>
                    <td>{t.schema_name}.{t.table_name}</td>
                    <td>{t.last_autovacuum ? fmtLag(t.lag_sec) : "hiç vacuum yok"}</td>
                    <td>{t.freeze_age.toLocaleString()}</td>
                    <td>{t.dead_tup.toLocaleString()}</td>
                    <td><span className={`insight-severity ${t.severity}`}>{SEVERITY_LABELS[t.severity as Severity]}</span></td>
                    <td className="ddl-cell">{t.vacuum_ddl && <CopyableAction command={t.vacuum_ddl} />}</td>
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
