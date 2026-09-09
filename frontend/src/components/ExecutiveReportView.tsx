import type { ExecutiveReport } from "../api";

const GRADE_TONE: Record<string, string> = {
  Sağlıklı: "ok",
  Dikkat: "warning",
  Riskli: "critical",
};

const RISK_TONE: Record<string, string> = { yüksek: "critical", orta: "warning", düşük: "info" };

function fmtDuration(seconds: number): string {
  if (!seconds) return "—";
  if (seconds < 90) return `${Math.round(seconds)} sn`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} dk`;
  return `${(seconds / 3600).toFixed(1)} sa`;
}

/**
 * Yönetici (müşteri) raporu görünümü — Faz 17 İŞ 3/5.
 *
 * Bu bileşen teknik bulgulara HİÇ erişmez; yalnızca backend'in teknik detaydan arındırdığı
 * `ExecutiveReport` yapısını gösterir. Sızıntı riski böylece tek bir yerde (backend) kalır.
 */
export default function ExecutiveReportView({ data }: { data: ExecutiveReport }) {
  // `ExecutiveReport` üretilen şemadan türetiliyor ve bu alanlar orada serbest sözlük
  // (`dict[str, Any]`) — üretilen tipte `unknown` değerli geliyor. Alan ADLARININ hizası
  // derleyici tarafından korunuyor; sözlüklerin İÇİ zaten şemasız olduğu için burada tek
  // seferlik gevşetiliyor. Gevşetme dört satırla sınırlı ve bileşenin geri kalanı okunur
  // kalıyor.
  const loose = (value: unknown): Record<string, any> => (value as Record<string, any>) || {};
  const availability = loose(data.availability);
  const backup = loose(data.backup);
  const sla = (data.sla || []).map(loose);
  const inventory = loose(data.inventory);
  const trend = loose(data.trend);
  const risks = (data.risks || []).map(loose);
  const recommendations = (data.recommendations || []).map(loose);
  const decisions = (data.decisions || []).map(loose);
  const workDone = loose(data.work_done);

  return (
    <div className="executive-report">
      <div className={`card exec-cover ${GRADE_TONE[data.grade] || "info"}`}>
        <div>
          <h2>{data.scope_label}</h2>
          <p className="muted-note">
            {data.period_label} rapor · {new Date(data.period_start).toLocaleDateString("tr-TR")} –{" "}
            {new Date(data.period_end).toLocaleDateString("tr-TR")}
          </p>
        </div>
        <div className="exec-grade">
          <span className="exec-grade-value">{data.grade}</span>
          <span className="exec-grade-reason">{data.grade_reason}</span>
        </div>
      </div>

      <div className="card">
        <h3 className="chart-title">Erişilebilirlik</h3>
        {availability.uptime_pct === null || availability.uptime_pct === undefined ? (
          <p className="muted-note">{availability.unknown_reason || "Bu dönem için ölçüm yapılamadı."}</p>
        ) : (
          <>
            <div className="stats-grid compact">
              <div className="stat-tile">
                <div className="stat-tile-label">Erişilebilirlik</div>
                <div className="stat-tile-value">%{availability.uptime_pct}</div>
              </div>
              <div className="stat-tile">
                <div className="stat-tile-label">Kesinti sayısı</div>
                <div className="stat-tile-value">{availability.outage_count}</div>
              </div>
              <div className="stat-tile">
                <div className="stat-tile-label">Toplam kesinti</div>
                <div className="stat-tile-value">{fmtDuration(availability.total_outage_seconds)}</div>
              </div>
              <div className="stat-tile">
                <div className="stat-tile-label">En uzun kesinti</div>
                <div className="stat-tile-value">{fmtDuration(availability.longest_outage_seconds)}</div>
              </div>
            </div>
            {(availability.by_application || []).length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Uygulama</th>
                      <th>Erişilebilirlik</th>
                      <th>Kesinti</th>
                      <th>Toplam süre</th>
                    </tr>
                  </thead>
                  <tbody>
                    {availability.by_application.map((row: any) => (
                      <tr key={row.application}>
                        <td>{row.application}</td>
                        <td>{row.uptime_pct === null ? "—" : `%${row.uptime_pct}`}</td>
                        <td>{row.outage_count}</td>
                        <td>{fmtDuration(row.outage_seconds)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

      {/*
        Hizmet seviyesi (Faz 28 İŞ 3b). "Kalan kesinti bütçesi" çıplak yüzdeden çok daha
        anlamlı: ayın 3'ünde "%99.2" görmek hiçbir şey söylemez, "47 dakikanız kaldı" ise
        doğrudan bakım planlamak için kullanılabilir.
      */}
      <div className="card">
        <h3 className="chart-title">Hizmet seviyesi (SLA)</h3>
        {sla.length === 0 ? (
          <p className="muted-note">
            Tanımlı bir erişilebilirlik hedefi yok; hedefe uygunluk değerlendirilemiyor.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Kapsam</th>
                  <th>Dönem</th>
                  <th>Durum</th>
                </tr>
              </thead>
              <tbody>
                {sla.map((row, i) => (
                  <tr key={i}>
                    <td>{row.scope_label}</td>
                    <td>{row.period_label || "—"}</td>
                    <td className={row.met === false ? "warn-text" : undefined}>{row.statement}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/*
        Yedek güvencesi (Faz 28 İŞ 1b). Bulgu OLMASA DA gösteriliyor: yöneticinin sorduğu soru
        "sorun var mı" değil "yedeğim var mı" ve bu sorunun cevabı yalnızca kötü haber
        olduğunda görünürse rapor güvence vermiyor demektir.

        Üç durum var, iki değil: uygun, uygun değil ve BELİRLENEMEDİ. Sonuncusunu "uygun
        değil" göstermek yanlış alarm, "uygun" göstermek sahte güvence olurdu.
      */}
      <div className="card">
        <h3 className="chart-title">Yedek güvencesi</h3>
        <p className={backup.sla_ok === false ? "exec-backup-alert" : "muted-note"}>
          {backup.statement || "Bu dönemde yedek durumu değerlendirilemedi."}
        </p>
        {backup.measured && (
          <div className="stats-grid compact">
            <div className="stat-tile">
              <div className="stat-tile-label">Değerlendirilen veritabanı</div>
              <div className="stat-tile-value">{backup.database_count ?? 0}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Hedefe uygun</div>
              <div className="stat-tile-value">{backup.protected_count ?? 0}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Hedefin dışında</div>
              <div className="stat-tile-value">{backup.breached_count ?? 0}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Belirlenemedi</div>
              <div className="stat-tile-value">{backup.unknown_count ?? 0}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">En eski yedek</div>
              <div className="stat-tile-value">
                {backup.oldest_backup_days === null || backup.oldest_backup_days === undefined
                  ? "—"
                  : `${Math.round(backup.oldest_backup_days)} gün önce`}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <h3 className="chart-title">Sistem envanteri</h3>
        <div className="stats-grid compact">
          <div className="stat-tile">
            <div className="stat-tile-label">İzlenen veritabanı</div>
            <div className="stat-tile-value">{inventory.database_count ?? 0}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Veritabanı kümesi</div>
            <div className="stat-tile-value">{inventory.group_count ?? 0}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Ortamlar</div>
            <div className="stat-tile-sub">
              {Object.entries(inventory.environments || {})
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ") || "—"}
            </div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Topolojiler</div>
            <div className="stat-tile-sub">
              {Object.entries(inventory.topologies || {})
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ") || "—"}
            </div>
          </div>
        </div>
        {inventory.dr_coverage_note && <p className="muted-note">{inventory.dr_coverage_note}</p>}
      </div>

      <div className="card">
        <h3 className="chart-title">Risk özeti</h3>
        {risks.length === 0 ? (
          <p className="ok-text">Bu dönemde tespit edilmiş bir risk yok.</p>
        ) : (
          <div className="risk-list">
            {risks.map((risk, i) => (
              <div key={i} className={`risk-item ${RISK_TONE[risk.level] || "info"}`}>
                <div className="risk-head">
                  <span className={`insight-severity ${RISK_TONE[risk.level] || "info"}`}>
                    {String(risk.level).toUpperCase()}
                  </span>
                  <strong>{risk.area}</strong>
                  <span className="muted-note">{risk.application}</span>
                </div>
                <p>{risk.statement}</p>
                <p className="muted-note">{risk.business_impact}</p>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Ek İŞ A: planlanan çalışmalar ve bilinçli kabul edilen riskler. Yoksayılanlar
          buraya HİÇ girmez — o, ekibin kendi iç gürültü yönetimi kararı. */}
      <div className="card">
        <h3 className="chart-title">Planlanan çalışmalar ve kabul edilen riskler</h3>
        {decisions.length === 0 ? (
          <p className="muted-note">Bu dönemde planlanmış çalışma ya da kabul edilmiş risk yok.</p>
        ) : (
          <div className="risk-list">
            {decisions.map((item, i) => (
              <div key={i} className={`risk-item ${item.kind === "planned" ? "info" : "warning"}`}>
                <div className="risk-head">
                  <span className={`insight-severity ${item.kind === "planned" ? "info" : "warning"}`}>
                    {item.label}
                  </span>
                  <strong>{item.area}</strong>
                  <span className="muted-note">{item.application}</span>
                  {item.reference && <span className="tag">{item.reference}</span>}
                </div>
                <p>{item.statement}</p>
                <p className="muted-note">{item.business_impact}</p>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <h3 className="chart-title">Önceki döneme göre</h3>
        {!trend.available ? (
          <p className="muted-note">{trend.note || "Karşılaştırma yapılamadı."}</p>
        ) : (
          <>
            <p className={trend.direction === "iyileşti" ? "ok-text" : trend.direction === "kötüleşti" ? "warn-text" : ""}>
              {trend.note}
            </p>
            <div className="trend-bars">
              {[
                { label: "Yüksek riskli konu", prev: trend.previous.critical, now: trend.current.critical },
                { label: "Orta riskli konu", prev: trend.previous.warning, now: trend.current.warning },
              ].map((row) => {
                const max = Math.max(row.prev, row.now, 1);
                return (
                  <div key={row.label} className="trend-row">
                    <span className="trend-label">{row.label}</span>
                    <span className="trend-bar-wrap">
                      <span className="trend-bar prev" style={{ width: `${(row.prev / max) * 100}%` }} />
                      <span className="trend-bar now" style={{ width: `${(row.now / max) * 100}%` }} />
                    </span>
                    <span className="trend-values">
                      {row.prev} → {row.now}
                    </span>
                  </div>
                );
              })}
            </div>
            <p className="muted-note">
              Erişilebilirlik: {trend.previous.uptime_pct === null ? "—" : `%${trend.previous.uptime_pct}`} →{" "}
              {trend.current.uptime_pct === null ? "—" : `%${trend.current.uptime_pct}`}
            </p>
          </>
        )}
      </div>

      {/*
        Bu dönemde yapılanlar (Faz 28 İŞ 5). Müşteriye DBA ekibinin çalıştığını gösteren şey
        bu — emeğin görünmemesi, hizmetin değerinin de görünmemesi demek.

        Yönetici raporunda YALNIZCA sayılar ve kategoriler var; kim ne yaptı teknik raporda
        kalıyor. Müşteriye giden bir belgede kişi adı, hizmetin değil bireyin
        değerlendirilmesine dönüşür.
      */}
      <div className="card">
        <h3 className="chart-title">Bu dönemde yapılanlar</h3>
        <p>{workDone.note}</p>
        <div className="stats-grid compact">
          <div className="stat-tile">
            <div className="stat-tile-label">Açılan konu</div>
            <div className="stat-tile-value">{workDone.opened_findings ?? 0}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Kapatılan</div>
            <div className="stat-tile-value">{workDone.closed_findings ?? 0}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Çalışma planlanan</div>
            <div className="stat-tile-value">{workDone.planned_findings ?? 0}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Risk kabul edilen</div>
            <div className="stat-tile-value">{workDone.risk_accepted_findings ?? 0}</div>
          </div>
          {/* Tekrar açılanlar saklanmıyor: uygulanan çözümün işe yaramadığını gizlemek,
              raporu satış aracına çevirmek olurdu. */}
          <div className="stat-tile">
            <div className="stat-tile-label">Tekrar açılan</div>
            <div className="stat-tile-value">{workDone.reopened_findings ?? 0}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-tile-label">Ortalama çözüm süresi</div>
            <div className="stat-tile-value">
              {workDone.average_resolution_days == null
                ? "—"
                : `${workDone.average_resolution_days} gün`}
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <h3 className="chart-title">Öneriler</h3>
        {recommendations.length === 0 ? (
          <p className="muted-note">Bu dönem için aksiyon gerektiren bir öneri yok.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Öncelik</th>
                  <th>Alan</th>
                  <th>Uygulama</th>
                  <th>Önerilen aksiyon</th>
                  <th>Yapılmazsa</th>
                </tr>
              </thead>
              <tbody>
                {recommendations.map((rec, i) => (
                  <tr key={i}>
                    <td>
                      <span className={`insight-severity ${RISK_TONE[rec.priority] || "info"}`}>{rec.priority}</span>
                    </td>
                    <td>{rec.area}</td>
                    <td>{rec.application}</td>
                    <td>{rec.action}</td>
                    <td className="muted-note">{rec.if_not_done}</td>
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
