import { useEffect, useState } from "react";
import { api, type WaitStatEntry, type WaitStatsReport } from "../api";

/**
 * SQL Server bekleme istatistikleri (Faz 31 Commit 9, madde 5).
 *
 * İki sütun ayrı tutuluyor, çünkü aynı şey DEĞİLLER:
 * - **Fark**: son okumadan bu yana. "Şu anda ne bekliyoruz?" sorusunun cevabı bu.
 * - **Açılıştan beri**: kümülatif toplam. Uzun vadeli eğilim; tek başına bakıldığında yanıltıcı,
 *   çünkü haftalardır açık bir sunucuda eski bir olay hâlâ ilk sırada durabiliyor.
 *
 * Arka plan görevleri (kullanıcı oturumlarında hiç görülmeyen türler) varsayılan olarak eleniyor;
 * ELENEN SAYI ekranda yazıyor ve tek tıkla gösteriliyor — sessizce gizlenmiyor.
 */
function WaitTable({ rows }: { rows: WaitStatEntry[] }) {
  const total = rows.reduce((sum, row) => sum + row.wait_ms, 0) || 1;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Bekleme türü</th>
            <th>Süre (ms)</th>
            <th>Pay</th>
            <th>Bekleyen görev</th>
            <th>Ortalama (ms)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.wait_type}>
              <td>
                {row.wait_type}
                {row.is_background && <span className="muted-note"> · arka plan</span>}
              </td>
              <td>{Math.round(row.wait_ms).toLocaleString("tr-TR")}</td>
              <td>%{((row.wait_ms / total) * 100).toFixed(1)}</td>
              <td>{row.waiting_tasks.toLocaleString("tr-TR")}</td>
              <td>{row.avg_wait_ms.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function WaitStatsPanel({ instanceId }: { instanceId: number }) {
  const [report, setReport] = useState<WaitStatsReport | null>(null);
  const [includeBackground, setIncludeBackground] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getWaitStats(instanceId, includeBackground)
      .then((data) => {
        if (cancelled) return;
        setReport(data);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(String((e as Error).message || e));
      });
    return () => {
      cancelled = true;
    };
  }, [instanceId, includeBackground]);

  if (error) {
    return (
      <div className="card">
        <h3 className="chart-title">Bekleme istatistikleri</h3>
        <p className="muted-note">Ölçülemedi: {error}</p>
      </div>
    );
  }
  if (!report) return null;

  if (report.unavailable_reason) {
    return (
      <div className="card">
        <h3 className="chart-title">Bekleme istatistikleri</h3>
        <p className="muted-note">{report.unavailable_reason}</p>
        {report.required_grant && (
          <>
            <p className="muted-note">Gereken yetki (DBA çalıştırır):</p>
            <pre className="query-text">{report.required_grant}</pre>
          </>
        )}
      </div>
    );
  }

  const deltaSince = report.delta_since ? new Date(report.delta_since).toLocaleString("tr-TR") : null;
  return (
    <div className="card">
      <h3 className="chart-title">Bekleme istatistikleri</h3>
      <p className="muted-note">
        Sunucu açılışı:{" "}
        {report.server_start_time ? new Date(report.server_start_time).toLocaleString("tr-TR") : "bilinmiyor"}
        {report.filtered_background > 0 && (
          <>
            {" · "}
            {report.filtered_background} arka plan türü elendi (kullanıcı oturumlarında hiç görülmedi)
          </>
        )}
        {" · "}
        <button
          type="button"
          className="btn linkish"
          onClick={() => setIncludeBackground(!includeBackground)}
        >
          {includeBackground ? "arka planı gizle" : "arka planı da göster"}
        </button>
      </p>

      <h4 className="chart-title">Son okumadan bu yana</h4>
      {report.delta_unavailable_reason ? (
        <p className="muted-note">{report.delta_unavailable_reason}</p>
      ) : report.delta.length === 0 ? (
        <p className="muted-note">Son okumadan bu yana kayda değer bekleme olmadı.</p>
      ) : (
        <>
          {deltaSince && <p className="muted-note">Karşılaştırma anı: {deltaSince}</p>}
          <WaitTable rows={report.delta} />
        </>
      )}

      <h4 className="chart-title">Sunucu açılışından beri (kümülatif)</h4>
      {report.totals.length === 0 ? (
        <p className="muted-note">
          Kullanıcı oturumlarına atfedilen bekleme yok — ölçüm yapıldı, bekleme görülmedi.
        </p>
      ) : (
        <WaitTable rows={report.totals} />
      )}
    </div>
  );
}
