import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatTime, Prediction } from "../api";

export default function PredictionsPage() {
  const [items, setItems] = useState<Prediction[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api.getPredictions().then(setItems).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    load();
  }, []);

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Tahminler</h2>
          <p>Trend analizi ile oluşması muhtemel sorunlar (proaktif DBA)</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Zaman</th>
              <th>Instance</th>
              <th>Metrik</th>
              <th>Şimdi → Tahmin</th>
              <th>Önem</th>
              <th>Mesaj</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty">
                  Açık tahmin yok. Worker metrik topladıkça trendler burada görünür.
                </td>
              </tr>
            ) : (
              items.map((p) => (
                <tr key={p.id}>
                  <td>{formatTime(p.created_at)}</td>
                  <td>
                    <Link to={`/instances/${p.instance_id}`}>#{p.instance_id}</Link>
                  </td>
                  <td><code>{p.metric_key}</code></td>
                  <td>
                    {p.current_value.toFixed(1)} → {p.predicted_value.toFixed(1)}
                    <div style={{ color: "var(--muted)", fontSize: "0.75rem" }}>
                      eşik: {p.threshold} · güven: {(p.confidence * 100).toFixed(0)}%
                    </div>
                  </td>
                  <td><span className={`status ${p.severity === "critical" ? "alerting" : "warning"}`}>{p.severity}</span></td>
                  <td>{p.message}</td>
                  <td>
                    <button className="btn" onClick={() => api.ackPrediction(p.id).then(load)}>
                      Onayla
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
