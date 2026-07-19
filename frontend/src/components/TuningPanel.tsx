import type { PerformanceInsight, TuningReport } from "../api";
import { formatTime } from "../api";

const CATEGORY_LABELS: Record<string, string> = {
  memory: "Bellek / Cache",
  connections: "Bağlantılar",
  io: "I/O & Checkpoint",
  queries: "Sorgular",
  replication: "Replikasyon",
  tuning: "Genel Tuning",
  collection: "Veri Toplama",
};

const SEVERITY_TR: Record<string, string> = {
  critical: "Kritik",
  high: "Yüksek",
  medium: "Orta",
  low: "Düşük",
  info: "Bilgi",
};

const ACTION_LABEL: Record<string, string> = {
  explain: "EXPLAIN plan aç",
  index_advice: "Index önerisi çalıştır",
  analyze: "Sorguyu incele",
};

type Props = {
  report: TuningReport | null;
  onOpenTab: (tab: "queries" | "metrics" | "alerts") => void;
  onFocusQuery?: (insight: PerformanceInsight) => void;
  onRunIndexAdvice: () => void;
  adviceRunning?: boolean;
};

export default function TuningPanel({
  report,
  onOpenTab,
  onFocusQuery,
  onRunIndexAdvice,
  adviceRunning,
}: Props) {
  if (!report) {
    return (
      <div className="card tuning-card">
        <div className="empty">Tuning raporu yükleniyor…</div>
      </div>
    );
  }

  const insights = report.insights || [];
  const byCategory = insights.reduce<Record<string, typeof insights>>((acc, item) => {
    const key = item.category || "tuning";
    (acc[key] ||= []).push(item);
    return acc;
  }, {});

  const issueCount =
    (report.summary.critical || 0) + (report.summary.high || 0) + (report.summary.medium || 0);

  return (
    <div className="tuning-layout">
      <section className="card tuning-hero">
        <div className={`tuning-score grade-${report.grade.toLowerCase()}`}>
          <div className="tuning-score-ring" style={{ ["--score" as string]: report.health_score }}>
            <span className="tuning-score-value">{report.health_score}</span>
            <span className="tuning-score-label">Sağlık</span>
          </div>
          <div className="tuning-score-meta">
            <strong>Not: {report.grade}</strong>
            <span className={`tuning-status ${report.status}`}>{report.status}</span>
            <p>
              {report.collected_at
                ? `Son metrik: ${formatTime(report.collected_at)}`
                : "Henüz metrik toplanmadı"}
            </p>
          </div>
        </div>

        <div className="tuning-summary-grid">
          {(["critical", "high", "medium", "info"] as const).map((sev) => (
            <div key={sev} className={`tuning-summary-chip ${sev}`}>
              <strong>{report.summary[sev] || 0}</strong>
              <span>{SEVERITY_TR[sev]}</span>
            </div>
          ))}
        </div>

        <div className="tuning-actions">
          <button className="btn primary" onClick={() => onOpenTab("queries")}>
            Yavaş sorgulara git
          </button>
          <button className="btn" onClick={onRunIndexAdvice} disabled={adviceRunning}>
            {adviceRunning ? "Index önerisi çalışıyor…" : "Top sorgulara index öner"}
          </button>
          <button className="btn" onClick={() => onOpenTab("metrics")}>
            Metrikleri aç
          </button>
        </div>

        <p className="tuning-hero-note">
          {issueCount > 0
            ? `${issueCount} aksiyon gerektiren bulgu var. Önce kritik/yüksek öncelikli maddelerden başlayın.`
            : "Kritik bulgu yok. Periyodik index ve sorgu kontrolü ile sağlığı koruyun."}
        </p>
      </section>

      <section className="card tuning-checklist-card">
        <h3 className="chart-title">DBA kontrol listesi</h3>
        <div className="tuning-checklist">
          {report.checklist.map((item) => (
            <div key={item.key} className={`checklist-row ${item.status}`}>
              <span className="checklist-status">{item.status}</span>
              <div>
                <strong>{item.label}</strong>
                <p>{item.detail}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="tuning-categories">
        {Object.entries(byCategory).map(([category, items]) => (
          <div key={category} className="card tuning-category-card">
            <h3 className="chart-title">{CATEGORY_LABELS[category] || category}</h3>
            <div className="insights-list">
              {items.map((insight) => (
                <div key={`${insight.category}-${insight.title}`} className={`insight-row ${insight.severity}`}>
                  <span className="insight-dot" />
                  <div className="insight-body">
                    <div className="insight-title">
                      <strong>{insight.title}</strong>
                      <span className={`insight-severity ${insight.severity}`}>
                        {SEVERITY_TR[insight.severity] || insight.severity}
                      </span>
                    </div>
                    <p>{insight.description}</p>

                    {(insight.query_hint || insight.queryid) && (
                      <div className="insight-query-block">
                        {insight.queryid && (
                          <div className="insight-query-meta">
                            <span>İlgili sorgu</span>
                            <code>queryid={insight.queryid}</code>
                          </div>
                        )}
                        {insight.query_hint && <pre className="insight-query-sql">{insight.query_hint}</pre>}
                      </div>
                    )}

                    <div className="insight-recommendation">
                      <span>Aksiyon:</span> {insight.recommendation}
                    </div>

                    <div className="insight-actions">
                      {insight.action && insight.action !== "none" && (
                        <button
                          className="btn linkish"
                          onClick={() => {
                            if (insight.action === "queries") onOpenTab("queries");
                            else if (insight.action === "metrics") onOpenTab("metrics");
                            else if (insight.action === "alerts") onOpenTab("alerts");
                          }}
                        >
                          İlgili sekmeye git →
                        </button>
                      )}
                      {(insight.queryid || insight.query_hint) && onFocusQuery && (
                        <button className="btn primary linkish" onClick={() => onFocusQuery(insight)}>
                          {ACTION_LABEL[insight.suggested_action || ""] || "Bu sorguya git →"}
                        </button>
                      )}
                    </div>
                  </div>
                  {insight.metric_value !== null && insight.metric_value !== undefined && (
                    <span className="insight-metric">
                      {Number(insight.metric_value).toFixed(1)} {insight.metric_unit}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}
