import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Advice,
  api,
  AppConfig,
  DashboardSummary,
  formatRelativeTime,
  GroupOverallStatus,
  HealthReportSummary,
  HealthResponse,
} from "../api";
import { useAuth } from "../auth";
import { PageError, PageSkeleton } from "../components/PageState";
import { useUrlFilter } from "../hooks/useUrlState";
import AdviceCard from "../components/AdviceCard";
import CopyableAction from "../components/CopyableAction";
import RecommendationHeader from "../components/RecommendationHeader";

const STATUS_LABELS_TR: Record<GroupOverallStatus, string> = {
  critical: "Kritik",
  warning: "Uyarı",
  healthy: "Sağlıklı",
  unknown: "Bilinmiyor",
};

interface ProblemCard {
  key: string;
  severity: string;
  title: string;
  recTitle: string | null;
  customer: string;
  application: string;
  group: string;
  node: string | null;
  environment: string;
  linkHint: string;
  checkedAt: string | null;
  steps: string[];
  action: string | null;
  /** Faz 17 Ek İŞ B: standart öneri yapısı; steps/action geriye dönük uyumluluk için. */
  advice: Advice | null;
}

// Merges top_issues (a concrete problem, may carry the group's best-matching recommendation)
// and recommendations (may exist for a group with no active "issue" — e.g. a parameter_audit
// finding on an otherwise healthy group) into one deduplicated, card-per-row list (Faz 15 İŞ
// 4) — a recommendation already attached to an issue isn't repeated as its own card.
function buildProblemCards(summary: DashboardSummary): ProblemCard[] {
  const seen = new Set<string>();
  const cards: ProblemCard[] = [];

  summary.top_issues.forEach((issue, idx) => {
    const rec = issue.recommendation;
    if (rec) seen.add(`${rec.group}|${rec.message}`);
    cards.push({
      key: `issue-${idx}`,
      severity: issue.severity,
      title: issue.message,
      customer: issue.customer,
      application: issue.application,
      group: issue.group,
      node: issue.node ?? null,
      environment: issue.environment,
      // Üretilen tipte bu alanlar opsiyonel; kart modeli `string | null` bekliyor.
      linkHint: issue.link_hint ?? null,
      checkedAt: issue.checked_at ?? null,
      recTitle: rec?.title ?? null,
      steps: rec?.steps ?? [],
      action: rec?.action ?? null,
      advice: rec?.advice ?? null,
    });
  });

  summary.recommendations.forEach((rec, idx) => {
    const key = `${rec.group}|${rec.message}`;
    if (seen.has(key)) return;
    seen.add(key);
    cards.push({
      key: `rec-${idx}`,
      severity: rec.severity,
      title: rec.message,
      recTitle: rec.title ?? null,
      customer: rec.customer,
      application: rec.application,
      group: rec.group,
      node: null,
      environment: rec.environment,
      linkHint: rec.link_hint ?? null,
      checkedAt: rec.checked_at ?? null,
      steps: rec.steps ?? [],
      action: rec.action ?? null,
      advice: rec.advice ?? null,
    });
  });

  return cards;
}

export default function DashboardPage() {
  const canWrite = useAuth().user?.role === "admin";
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<HealthResponse | null>(null);

  const [groupSummary, setGroupSummary] = useState<DashboardSummary | null>(null);
  const [groupSummaryError, setGroupSummaryError] = useState<string | null>(null);
  const [groupSummaryLoading, setGroupSummaryLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // Faz 17 İŞ 5: dashboard'daki "bugünün raporu" kartı.
  const [latestReport, setLatestReport] = useState<HealthReportSummary | null>(null);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  // The interval value itself is now only changeable from the admin screen (Faz 15 İŞ 2) —
  // still read here so the auto-refresh timer below uses whatever's currently configured.
  const [refreshSeconds, setRefreshSeconds] = useState<number | null>(null);
  // Durum filtresi adreste tutuluyor: eskiden yalniz bilesen state'indeydi, bu yuzden
  // filtrelenmis gorunum paylasilamiyor ve geri dugmesi filtreyi kaldirmak yerine
  // kullaniciyi sayfadan atiyordu (Faz 19 IS 2).
  // Kart tıklaması bilinçli bir gezinme adımı: geri düğmesi filtreyi kaldırsın (bkz. hook).
  const [statusFilterParam, setStatusFilterParam] = useUrlFilter("status", "", { history: "push" });
  const statusFilter = (statusFilterParam || null) as GroupOverallStatus | null;
  const setStatusFilter = (next: GroupOverallStatus | null) => setStatusFilterParam(next ?? "");
  const [openCards, setOpenCards] = useState<Set<string>>(new Set());

  const loadSummary = () => {
    setGroupSummaryLoading(true);
    setGroupSummaryError(null);
    api.getDashboardSummary()
      .then(setGroupSummary)
      .catch((err) => setGroupSummaryError(String(err.message || err)))
      .finally(() => setGroupSummaryLoading(false));
  };

  useEffect(() => {
    api.getHealth().then(setConfig).catch((err) => setError(String(err.message || err)));
    api.getConfig().then(setAppConfig).catch(() => undefined);
    loadSummary();
    api.getRefreshInterval().then((r) => setRefreshSeconds(r.seconds)).catch(() => undefined);
    api.getLatestReport("global").then(setLatestReport).catch(() => undefined);
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

  const isPrivate = config?.deployment_mode === "private";
  const isPrivateGroups = appConfig?.deployment_mode === "private";
  const problemCards = groupSummary ? buildProblemCards(groupSummary) : [];

  const toggleCard = (key: string) => {
    setOpenCards((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const StatCard = ({
    label,
    value,
    color,
    sub,
    status,
  }: {
    label: string;
    value: string | number;
    color?: string;
    sub?: string;
    status?: GroupOverallStatus;
  }) => {
    const content = (
      <div
        className={`card stat-card${status ? " clickable" : ""}${status && statusFilter === status ? " active" : ""}`}
        style={{ borderLeftColor: color || "var(--accent)" }}
      >
        <div className="stat-meta">
          <h3>{label}</h3>
          {sub && <span>{sub}</span>}
        </div>
        <div className="value" style={{ color: color || "var(--text)" }}>{value}</div>
      </div>
    );
    if (!status) return content;
    return (
      <button
        type="button"
        className="stat-card-btn"
        onClick={() => setStatusFilter(statusFilter === status ? null : status)}
      >
        {content}
      </button>
    );
  };

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

      {error && <PageError error={error} onRetry={() => { setError(null); api.getHealth().then(setConfig).catch((err) => setError(String(err.message || err))); }} />}
      {groupSummaryError && <PageError error={groupSummaryError} onRetry={loadSummary} />}

      {/* Faz 17 İŞ 5: bugünün sağlık raporu kartı — tıklayınca doğrudan o rapora gider. */}
      {latestReport && (
        <Link
          to={`/reports?report=${latestReport.id}`}
          className={`card report-teaser ${latestReport.overall_status}`}
        >
          <div>
            <strong>Bugünün sağlık raporu</strong>
            <p className="muted-note">
              {latestReport.scope_label} · {formatRelativeTime(latestReport.generated_at)}
            </p>
          </div>
          <div className="report-teaser-counts">
            <span className="report-teaser-critical">{latestReport.critical_count}</span>
            <span className="muted-note">kritik bulgu</span>
            <span className="muted-note">· {latestReport.warning_count} uyarı</span>
          </div>
          <span className="report-teaser-cta">Raporu aç →</span>
        </Link>
      )}

      <div className="activity-toolbar" style={{ marginBottom: "1rem" }}>
        <span className="muted-note">
          {groupSummary?.last_checked
            ? `Son güncelleme: ${formatRelativeTime(groupSummary.last_checked)}`
            : groupSummaryLoading
              ? "Yükleniyor…"
              : "Henüz sağlık verisi toplanmadı"}
        </span>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
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
              status="critical"
            />
            <StatCard
              label="Uyarı"
              value={groupSummary?.health.warning ?? 0}
              color="var(--warning)"
              sub="grup bazında"
              status="warning"
            />
            <StatCard
              label="Sağlıklı"
              value={groupSummary?.health.healthy ?? 0}
              color="var(--success)"
              sub="grup bazında"
              status="healthy"
            />
            <StatCard
              label="Bilinmiyor"
              value={groupSummary?.health.unknown ?? 0}
              color="var(--muted)"
              sub="veri yok / düğümsüz"
              status="unknown"
            />
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

          {statusFilter && (
            <div className="card" style={{ marginBottom: "1.5rem" }}>
              <div className="activity-toolbar">
                <h3 className="chart-title" style={{ margin: 0 }}>
                  {STATUS_LABELS_TR[statusFilter]} gruplar
                  <span className="muted-note" style={{ marginLeft: "0.5rem" }}>
                    ({(groupSummary?.groups ?? []).filter((g) => g.status === statusFilter).length})
                  </span>
                </h3>
                <button type="button" className="btn btn-xs" onClick={() => setStatusFilter(null)}>
                  Filtreyi temizle
                </button>
              </div>
              {(groupSummary?.groups ?? []).filter((g) => g.status === statusFilter).length === 0 ? (
                <p className="muted-note">Bu durumda grup yok.</p>
              ) : (
                <ul className="event-list">
                  {(groupSummary?.groups ?? [])
                    .filter((g) => g.status === statusFilter)
                    .map((g) => (
                      <li key={g.group_id}>
                        <span
                          className="event-dot"
                          style={{
                            background:
                              g.status === "critical"
                                ? "var(--danger)"
                                : g.status === "warning"
                                  ? "var(--warning)"
                                  : g.status === "healthy"
                                    ? "var(--success)"
                                    : "var(--muted)",
                          }}
                        />
                        <div>
                          <Link to={g.link_hint}>{g.group}</Link>
                          {!isPrivateGroups && <span className="muted-note"> · {g.customer} / {g.application}</span>}{" "}
                          <span className={`env-badge ${g.environment}`}>{g.environment}</span>
                        </div>
                      </li>
                    ))}
                </ul>
              )}
            </div>
          )}

          <div className="card" style={{ marginBottom: "1.5rem" }}>
            <h3 className="chart-title">Sorunlar ve öneriler</h3>
            {/* Tek satırlık "Yükleniyor…" yerine iskelet: kart yüksekliği veri gelince
                birden değişip altındaki içeriği aşağı itmiyor (Faz 22 İŞ 2). */}
            {groupSummaryLoading ? (
              <PageSkeleton rows={3} withHeader={false} />
            ) : problemCards.length === 0 ? (
              <p className="muted-note">Şu anda açık bir sorun veya öneri yok.</p>
            ) : (
              <div className="problem-card-list">
                {problemCards.map((card) => {
                  const isOpen = openCards.has(card.key);
                  const hasBody = card.steps.length > 0 || !!card.action;
                  return (
                    <div className="problem-card" key={card.key}>
                      <div className="problem-card-head">
                        <button
                          type="button"
                          className="problem-card-toggle"
                          disabled={!hasBody}
                          onClick={() => hasBody && toggleCard(card.key)}
                        >
                          <span className="problem-card-chevron">{hasBody ? (isOpen ? "▾" : "▸") : "·"}</span>
                          <span className={`insight-severity ${card.severity}`}>{card.severity}</span>
                          <span className="problem-card-title">{card.title}</span>
                        </button>
                        <div className="problem-card-source muted-note">
                          <Link to={card.linkHint}>{card.group}</Link>
                          {!isPrivateGroups && card.customer && (
                            <span> · {card.customer} / {card.application}</span>
                          )}
                          {card.node && <span> · düğüm: {card.node}</span>}
                          {card.environment && <span className={`env-badge ${card.environment}`}>{card.environment}</span>}
                          {card.checkedAt && <span> · {formatRelativeTime(card.checkedAt)}</span>}
                        </div>
                      </div>
                      {isOpen && hasBody && (
                        <div className="problem-card-body">
                          {/* Faz 17 Ek İŞ B: rapor/DPA/tahminlerle aynı öneri bileşeni. Eski
                              alanlar yalnızca advice yoksa (eski snapshot) devreye girer. */}
                          {card.advice ? (
                            <AdviceCard advice={card.advice} />
                          ) : (
                            <>
                              {card.recTitle && <RecommendationHeader title={card.recTitle} />}
                              {card.steps.length > 0 && (
                                <ol>
                                  {card.steps.map((step, i) => (
                                    <li key={i}>{step}</li>
                                  ))}
                                </ol>
                              )}
                              {card.action && <CopyableAction command={card.action} />}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
