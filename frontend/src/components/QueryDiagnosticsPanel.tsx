import { useMemo, useState } from "react";
import type { QueryDiagnosticsReport, QueryResourceType } from "../api";
import { PageSkeleton } from "./PageState";

const TOP_N_OPTIONS = [5, 10, 20, 50] as const;

const RESOURCE_TABS: { key: QueryResourceType | "all"; label: string }[] = [
  { key: "all", label: "Toplam etki" },
  { key: "io", label: "I/O" },
  { key: "cpu", label: "CPU" },
  { key: "memory", label: "Bellek" },
  { key: "lock", label: "Kilit / Bekleme" },
  { key: "unknown", label: "Bilinmiyor" },
];

const RESOURCE_LABELS_TR: Record<QueryResourceType, string> = {
  io: "I/O",
  cpu: "CPU",
  memory: "Bellek",
  lock: "Kilit/Bekleme",
  unknown: "Bilinmiyor",
};

type Props = {
  data: QueryDiagnosticsReport | null;
  error: string | null;
  loading: boolean;
  topN: number;
  onTopNChange: (n: number) => void;
  onOpenQuery?: (queryid: string | null) => void;
};

export default function QueryDiagnosticsPanel({ data, error, loading, topN, onTopNChange, onOpenQuery }: Props) {
  const [filter, setFilter] = useState<QueryResourceType | "all">("all");

  const filtered = useMemo(() => {
    if (!data) return [];
    const rows = filter === "all" ? data.diagnoses : data.diagnoses.filter((d) => d.resource === filter);
    return [...rows].sort((a, b) => b.total_time_ms - a.total_time_ms);
  }, [data, filter]);

  return (
    <div className="card tuning-checklist-card">
      <div className="insights-header">
        <div>
          <h3 className="chart-title">Performans tuning — kaynak bazlı analiz</h3>
          <p style={{ color: "var(--muted)", fontSize: "0.8rem", margin: "0.25rem 0 0" }}>
            En son toplanan yavaş sorgu anlık görüntüsünden — her sorgu için darboğazın I/O, CPU,
            bellek ya da kilit/bekleme olduğu, hangi metriğin bunu gösterdiğiyle birlikte.
          </p>
        </div>
        <label style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.8rem" }}>
          Top
          <select value={topN} onChange={(e) => onTopNChange(Number(e.target.value))}>
            {TOP_N_OPTIONS.map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
      </div>

      {error && <div className="error">{error}</div>}
      {/* İskelet: tanı listesi gelince panel yüksekliği sıçramasın (Faz 22 İŞ 2). */}
      {loading && <PageSkeleton rows={3} withHeader={false} />}

      {data && (
        <>
          <p className="muted-note" style={{ margin: "0.5rem 0 0.75rem" }}>{data.server_resource_note}</p>

          <div className="detail-tabs" style={{ marginBottom: "0.75rem" }}>
            {RESOURCE_TABS.map((t) => {
              const count = t.key === "all" ? data.diagnoses.length : data.by_resource[t.key] || 0;
              return (
                <button
                  key={t.key}
                  className={`tab-btn${filter === t.key ? " active" : ""}`}
                  onClick={() => setFilter(t.key)}
                >
                  {t.label} ({count})
                </button>
              );
            })}
          </div>

          {filtered.length === 0 ? (
            <p className="empty">Bu kategoride sorgu yok.</p>
          ) : (
            <div className="tuning-checklist">
              {filtered.map((d, i) => (
                <div key={`${d.queryid}-${i}`} className={`checklist-row ${d.confidence === "inferred" ? "warn" : "ok"}`}>
                  <span className="checklist-status">{RESOURCE_LABELS_TR[d.resource]}</span>
                  <div style={{ flex: 1 }}>
                    <code style={{ display: "block", fontSize: "0.78rem", marginBottom: "0.25rem" }}>
                      {d.query.length > 160 ? `${d.query.slice(0, 160)}…` : d.query}
                    </code>
                    <p>{d.reason}</p>
                    <p style={{ color: "var(--muted)", fontSize: "0.75rem" }}>
                      {d.calls} çağrı · ortalama {d.mean_time_ms.toFixed(1)}ms · toplam {d.total_time_ms.toFixed(0)}ms
                      {d.confidence === "inferred" && " · çıkarım (kesin ölçüm değil)"}
                    </p>
                    {onOpenQuery && d.queryid && (
                      <button className="btn linkish" onClick={() => onOpenQuery(d.queryid)}>
                        Yavaş sorgu detayına git →
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
