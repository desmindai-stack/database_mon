import type { PrerequisiteCheck, PrerequisiteReport } from "../api";
import { formatTime } from "../api";
import CopyableAction from "./CopyableAction";

const STATUS_TR: Record<PrerequisiteCheck["status"], string> = {
  ok: "Tamam",
  missing: "Eksik",
  unauthorized: "Yetkisiz",
  unknown: "Bilinmiyor",
};

// checklist-row only styles ok/warn/critical (see index.css, Tuning tab) — map our four
// statuses onto those three visual buckets rather than inventing a parallel palette.
function rowClass(check: PrerequisiteCheck): string {
  if (check.status === "ok") return "ok";
  if (check.status === "unknown") return "warn";
  return "critical"; // missing | unauthorized
}

type Props = {
  data: PrerequisiteReport | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
};

export default function PrerequisitesPanel({ data, error, loading, onRefresh }: Props) {
  return (
    <div className="card tuning-checklist-card">
      <div className="insights-header">
        <div>
          <h3 className="chart-title">Ön koşullar</h3>
          <p style={{ color: "var(--muted)", fontSize: "0.8rem", margin: "0.2rem 0 0" }}>
            Yavaş sorgu / EXPLAIN / index önerisi özelliklerinin çalışması için gereken uzantı, ayar ve
            yetkiler — biri eksikse ilgili özellik neden boş göründüğünü burada görürsünüz.
          </p>
        </div>
        <button className="btn" onClick={onRefresh} disabled={loading}>
          {loading ? "Kontrol ediliyor…" : "Yeniden kontrol et"}
        </button>
      </div>

      {error && <div className="error">{error}</div>}

      {!data && !error && !loading && <div className="empty">Ön koşullar henüz kontrol edilmedi.</div>}

      {data && (
        <>
          <p style={{ color: "var(--muted)", fontSize: "0.8rem", margin: "0 0 0.6rem" }}>
            {data.ok_count}/{data.checks.length} kontrol tamam · Son kontrol: {formatTime(data.checked_at)}
          </p>
          <div className="tuning-checklist">
            {data.checks.map((check) => (
              <div key={check.key} className={`checklist-row ${rowClass(check)}`}>
                <span className="checklist-status">{STATUS_TR[check.status]}</span>
                <div style={{ flex: 1 }}>
                  <strong>{check.name}</strong>
                  <p>{check.impact}</p>
                  {check.detail && (
                    <p style={{ color: "var(--muted)" }}>Mevcut değer: {check.detail}</p>
                  )}
                  {check.fix && (
                    <div style={{ marginTop: "0.4rem" }}>
                      <CopyableAction command={check.fix} />
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
