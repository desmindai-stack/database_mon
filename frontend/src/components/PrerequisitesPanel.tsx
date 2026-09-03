import type { PrerequisiteCheck, PrerequisiteReport } from "../api";
import { formatTime } from "../api";
import CopyableAction from "./CopyableAction";

const STATUS_TR: Record<PrerequisiteCheck["status"], string> = {
  ok: "Tamam",
  partial: "Kısıtlı",
  missing: "Eksik",
  unauthorized: "Yetkisiz",
  unknown: "Bilinmiyor",
};

// checklist-row only styles ok/warn/critical (see index.css, Tuning tab) — map our five
// statuses onto those three visual buckets rather than inventing a parallel palette.
// "partial" (kurulu ama kapsamı kısıtlı) uyarı; yeşil göstermek yanıltıcı olurdu.
function rowClass(check: PrerequisiteCheck): string {
  if (check.status === "ok") return "ok";
  if (check.status === "unknown" || check.status === "partial") return "warn";
  return "critical"; // missing | unauthorized
}

type Props = {
  data: PrerequisiteReport | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
  /** Faz 16-B İŞ 6: yoksayılan kontrollerin tam listesini kaydeder. */
  onSetIgnored?: (keys: string[]) => void;
  canWrite?: boolean;
};

export default function PrerequisitesPanel({
  data,
  error,
  loading,
  onRefresh,
  onSetIgnored,
  canWrite = false,
}: Props) {
  const active = data?.checks.filter((c) => !c.ignored) ?? [];
  const ignored = data?.checks.filter((c) => c.ignored) ?? [];

  const setIgnoredKeys = (key: string, next: boolean) => {
    if (!data || !onSetIgnored) return;
    const current = data.checks.filter((c) => c.ignored).map((c) => c.key);
    onSetIgnored(next ? [...current, key] : current.filter((k) => k !== key));
  };

  const renderRow = (check: PrerequisiteCheck) => (
    <div key={check.key} className={`checklist-row ${check.ignored ? "ignored" : rowClass(check)}`}>
      <span className="checklist-status">{STATUS_TR[check.status]}</span>
      <div style={{ flex: 1 }}>
        <strong>{check.name}</strong>
        <p>{check.impact}</p>
        {check.detail && <p style={{ color: "var(--muted)" }}>Mevcut değer: {check.detail}</p>}
        {check.ignored && check.status !== "ok" && (
          <p className="warn-text">
            Bu kontrol yoksayıldı — etkilediği özellikler çalışmamaya devam eder, sadece ilerleme
            yüzdesine ve dashboard uyarılarına dahil edilmez.
          </p>
        )}
        {check.fix && !check.ignored && (
          <div style={{ marginTop: "0.4rem" }}>
            <CopyableAction command={check.fix} />
          </div>
        )}
      </div>
      {canWrite && onSetIgnored && (
        <button
          className="btn btn-xs"
          onClick={() => setIgnoredKeys(check.key, !check.ignored)}
          title={
            check.ignored
              ? "Bu kontrolü tekrar denetime dahil et"
              : "Bu kontrol bu ortamda geçerli değilse yoksay — yüzdeden düşer"
          }
        >
          {check.ignored ? "Geri al" : "Yoksay"}
        </button>
      )}
    </div>
  );

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
            {data.ok_count}/{active.length} kontrol tamam (%{data.completion_pct})
            {data.ignored_count > 0 && ` · ${data.ignored_count} yoksayıldı`} · Son kontrol:{" "}
            {formatTime(data.checked_at)}
          </p>
          <div className="tuning-checklist">{active.map(renderRow)}</div>

          {ignored.length > 0 && (
            <div className="ignored-section">
              <h4>Yoksayılan kontroller ({ignored.length})</h4>
              <p className="muted-note">
                Bunlar ilerleme yüzdesine dahil edilmez ve dashboard'da uyarı üretmez. Gerçek
                durumları aşağıda olduğu gibi görünmeye devam eder.
              </p>
              <div className="tuning-checklist">{ignored.map(renderRow)}</div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
