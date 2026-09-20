import type { PlanRegressionReport } from "../api";

/**
 * SQL Server plan regresyonu (Faz 31 Commit 9, madde 4).
 *
 * "Dün hızlıydı, bugün yavaş" sorusunun cevabı: aynı sorgunun ESKİ planı ile ŞİMDİKİ planı yan yana.
 * Query Store kapalıysa / okunamıyorsa boş liste göstermiyoruz — NEDEN ölçülemediği ve gereken
 * ayar/yetki komut olarak yazılıyor.
 */
export default function PlanRegressionPanel({ report }: { report: PlanRegressionReport | null }) {
  if (!report) return null;

  if (report.unavailable_reason) {
    return (
      <div className="card">
        <h3 className="chart-title">Plan regresyonu</h3>
        <p className="muted-note">{report.unavailable_reason}</p>
        {report.required_setting && (
          <>
            <p className="muted-note">Gereken ayar / yetki (DBA çalıştırır):</p>
            <pre className="query-text">{report.required_setting}</pre>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="card">
      <h3 className="chart-title">Plan regresyonu</h3>
      <p className="muted-note">
        Query Store durumu: {report.state} · {report.plans} plan kaydı ·{" "}
        {report.queries_with_history} sorgunun karşılaştırılabilir planı var ·{" "}
        {report.regression_count} regresyon.
      </p>
      {report.items.length === 0 ? (
        <p className="muted-note">
          Karşılaştırılan planlarda yavaşlama yok — plan değişiklikleri sorguyu yavaşlatmamış.
        </p>
      ) : (
        <ul className="deadlock-list">
          {report.items.map((item) => (
            <li key={`${item.query_id}-${item.current.plan_id}`} className="deadlock-item">
              <div className="muted-note">
                Sorgu #{item.query_id} · <strong>{item.slowdown_factor}× yavaşladı</strong>
                {item.current.is_forced && " · plan ZORLANMIŞ (forced)"}
              </div>
              <pre className="query-text">{item.query}</pre>
              <div className="query-stats-grid">
                <div>
                  <span>şimdiki plan</span>
                  <strong>
                    #{item.current.plan_id} · {item.current.avg_duration_ms.toFixed(1)} ms
                  </strong>
                </div>
                <div>
                  <span>önceki (en iyi) plan</span>
                  <strong>
                    #{item.baseline.plan_id} · {item.baseline.avg_duration_ms.toFixed(1)} ms
                  </strong>
                </div>
                <div>
                  <span>çalıştırma (şimdiki)</span>
                  <strong>{item.current.executions}</strong>
                </div>
                <div>
                  <span>mantıksal okuma</span>
                  <strong>
                    {item.baseline.avg_logical_reads?.toFixed(0) ?? "—"} → {item.current.avg_logical_reads?.toFixed(0) ?? "—"}
                  </strong>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
