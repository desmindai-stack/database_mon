import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, AppConfig, DashboardSummary, formatRelativeTime, HealthResponse } from "../api";
import { useAuth } from "../auth";

// Description ("mesaj") and the concrete follow-up command ("aksiyon") render on separate
// lines — the command is monospace and one click away from the clipboard, since it's meant to
// be pasted straight into a terminal/psql session rather than read as prose.
function CopyableAction({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API unavailable (permissions/non-secure context) — the command is still
      // visible and selectable by hand, so this is a silent no-op rather than an error.
    }
  };
  return (
    <div className="rec-action-row">
      <code className="rec-action-code">{command}</code>
      <button type="button" className="btn btn-xs" onClick={onCopy}>
        {copied ? "Kopyalandı" : "Kopyala"}
      </button>
    </div>
  );
}

const SEVERITY_COLOR: Record<string, string> = {
  critical: "var(--danger)",
  high: "var(--danger)",
  warning: "var(--warning)",
  medium: "var(--warning)",
  low: "var(--success)",
  info: "var(--success)",
};

const INTERVAL_LABELS: Record<number, string> = {
  10: "10 saniye",
  30: "30 saniye",
  60: "1 dakika",
  300: "5 dakika",
  900: "15 dakika",
  3600: "1 saat",
};

export default function DashboardPage() {
  const canWrite = useAuth().user?.role === "admin";
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<HealthResponse | null>(null);

  const [groupSummary, setGroupSummary] = useState<DashboardSummary | null>(null);
  const [groupSummaryError, setGroupSummaryError] = useState<string | null>(null);
  const [groupSummaryLoading, setGroupSummaryLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  const [refreshOptions, setRefreshOptions] = useState<number[]>([10, 30, 60, 300, 900, 3600]);
  const [refreshSeconds, setRefreshSeconds] = useState<number | null>(null);

  useEffect(() => {
    api.getHealth().then(setConfig).catch((err) => setError(String(err.message || err)));
    api.getConfig().then(setAppConfig).catch(() => undefined);
    setGroupSummaryLoading(true);
    api.getDashboardSummary()
      .then(setGroupSummary)
      .catch((err) => setGroupSummaryError(String(err.message || err)))
      .finally(() => setGroupSummaryLoading(false));
    api.getRefreshInterval().then((r) => {
      setRefreshOptions(r.options);
      setRefreshSeconds(r.seconds);
    }).catch(() => undefined);
  }, []);

  // Dashboard'ı seçilen aralıkta kendini otomatik güncelle — sadece önbellekten okur
  // (GET /summary), canlı probe scheduler'ın kendi işi (aynı aralık orada da geçerli).
  useEffect(() => {
    if (!refreshSeconds) return;
    const id = window.setInterval(() => {
      api.getDashboardSummary().then(setGroupSummary).catch(() => undefined);
    }, refreshSeconds * 1000);
    return () => window.clearInterval(id);
  }, [refreshSeconds]);

  const onRefresh = () => {
    setRefreshing(true);
    setGroupSummaryError(null);
    api.refreshDashboard()
      .then(setGroupSummary)
      .catch((err) => setGroupSummaryError(String(err.message || err)))
      .finally(() => setRefreshing(false));
  };

  const onChangeInterval = (seconds: number) => {
    setRefreshSeconds(seconds);
    api.setRefreshInterval(seconds).catch(() => undefined);
  };

  const isPrivate = config?.deployment_mode === "private";
  const isPrivateGroups = appConfig?.deployment_mode === "private";

  const StatCard = ({
    label,
    value,
    color,
    sub,
  }: {
    label: string;
    value: string | number;
    color?: string;
    sub?: string;
  }) => (
    <div className="card stat-card" style={{ borderLeftColor: color || "var(--accent)" }}>
      <div className="stat-meta">
        <h3>{label}</h3>
        {sub && <span>{sub}</span>}
      </div>
      <div className="value" style={{ color: color || "var(--text)" }}>{value}</div>
    </div>
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h2>DBA Overview</h2>
          <p>
            {isPrivate
              ? `${config?.default_customer_name || "Private"} ortamı — veritabanı gruplarının durum özeti`
              : "Tüm müşteri ve uygulamaların veritabanı grubu durum özeti"}
          </p>
        </div>
        <div className="header-actions">
          <Link to="/instances" className="btn">Tüm instance’lar</Link>
          {canWrite && <Link to="/customers" className="btn btn-primary">+ Yeni instance</Link>}
        </div>
      </header>

      {error && <div className="error">{error}</div>}
      {groupSummaryError && <div className="error">{groupSummaryError}</div>}

      <div className="activity-toolbar" style={{ marginBottom: "1rem" }}>
        <span className="muted-note">
          {groupSummary?.last_checked
            ? `Son güncelleme: ${formatRelativeTime(groupSummary.last_checked)}`
            : groupSummaryLoading
              ? "Yükleniyor…"
              : "Henüz sağlık verisi toplanmadı"}
        </span>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <label className="muted-note" style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            Otomatik yenileme
            <select
              value={refreshSeconds ?? ""}
              onChange={(e) => onChangeInterval(Number(e.target.value))}
            >
              {refreshOptions.map((s) => (
                <option key={s} value={s}>{INTERVAL_LABELS[s] ?? `${s}sn`}</option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={onRefresh} disabled={refreshing}>
            {refreshing ? "Yenileniyor…" : "Yenile"}
          </button>
        </div>
      </div>

      {!groupSummaryLoading && groupSummary && groupSummary.totals.groups === 0 ? (
        <div className="card empty-card" style={{ marginBottom: "1.5rem" }}>
          <p style={{ color: "var(--muted)" }}>
            Henüz izlenen bir database group yok. <Link to="/customers">İzlenecek grup ekleyin</Link>.
          </p>
        </div>
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="Kritik"
              value={groupSummary?.health.critical ?? 0}
              color="var(--danger)"
              sub="grup bazında"
            />
            <StatCard label="Uyarı" value={groupSummary?.health.warning ?? 0} color="var(--warning)" sub="grup bazında" />
            <StatCard label="Sağlıklı" value={groupSummary?.health.healthy ?? 0} color="var(--success)" sub="grup bazında" />
            <StatCard label="Bilinmiyor" value={groupSummary?.health.unknown ?? 0} color="var(--muted)" sub="veri yok / düğümsüz" />
            <StatCard
              label="Database groups"
              value={groupSummary?.totals.groups ?? 0}
              color="var(--accent)"
              sub={`${groupSummary?.totals.nodes ?? 0} düğüm`}
            />
            {!isPrivateGroups && (
              <StatCard label="Müşteriler" value={groupSummary?.totals.customers ?? 0} color="#a78bfa" />
            )}
          </div>

          <div className="grid grid-2" style={{ marginBottom: "1.5rem" }}>
            <div className="card">
              <h3 className="chart-title">En kritik sorunlar</h3>
              {groupSummaryLoading ? (
                <p className="muted-note">Yükleniyor…</p>
              ) : !groupSummary || groupSummary.top_issues.length === 0 ? (
                <p className="muted-note">Şu anda açık bir sorun yok.</p>
              ) : (
                <ul className="event-list">
                  {groupSummary.top_issues.map((issue, idx) => (
                    <li key={idx}>
                      <span
                        className="event-dot"
                        style={{ background: SEVERITY_COLOR[issue.severity] || "var(--muted)" }}
                      />
                      <div>
                        <Link to={issue.link_hint}>{issue.group}</Link>
                        {!isPrivateGroups && <span className="muted-note"> · {issue.customer}</span>}{" "}
                        <span className={`env-badge ${issue.environment}`}>{issue.environment}</span>
                        <div className="muted-note">{issue.message}</div>
                        {issue.recommendation && (
                          <div className="insight-recommendation">
                            <div>
                              <span className={`insight-severity ${issue.recommendation.severity}`}>
                                öneri
                              </span>{" "}
                              {issue.recommendation.message}
                            </div>
                            {issue.recommendation.action && <CopyableAction command={issue.recommendation.action} />}
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="card">
              <h3 className="chart-title">Öneriler</h3>
              {groupSummaryLoading ? (
                <p className="muted-note">Yükleniyor…</p>
              ) : !groupSummary || groupSummary.recommendations.length === 0 ? (
                <p className="muted-note">Şu anda öneri yok.</p>
              ) : (
                <ul className="event-list">
                  {groupSummary.recommendations.map((rec, idx) => (
                    <li key={idx}>
                      <span className={`insight-severity ${rec.severity}`}>{rec.severity}</span>
                      <div>
                        <strong>{rec.group}</strong> <span className="muted-note">({rec.source})</span>
                        <div className="muted-note">{rec.message}</div>
                        {rec.action && <CopyableAction command={rec.action} />}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}
