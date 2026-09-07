import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage, formatTime, Prediction } from "../api";
import { TableState } from "../components/PageState";
import { useAuth } from "../auth";
import AdviceCard from "../components/AdviceCard";
import CopyableAction from "../components/CopyableAction";
import PredictionPlaybook from "../components/PredictionPlaybook";
import RecommendationHeader from "../components/RecommendationHeader";

export default function PredictionsPage() {
  const canWrite = useAuth().user?.role === "admin";
  const [items, setItems] = useState<Prediction[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);

  const load = () =>
    api
      .getPredictions()
      .then((rows) => {
        setItems(rows);
        setListError(null);
      })
      .catch(setListError)
      .finally(() => setLoaded(true));

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
              <th>Önerilen aksiyon</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <TableState
                colSpan={8}
                loading={!loaded}
                error={listError}
                onRetry={load}
                title="Açık tahmin yok"
                detail="Tahminler geçmiş metriklerin trendinden üretilir; yeterli örnek biriktikçe burada görünür. Yeni eklenen bir instance için genelde birkaç saat gerekir."
              />
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
                      {p.lower_bound !== null && p.upper_bound !== null && (
                        <> · %90 aralık: [{p.lower_bound.toFixed(1)} – {p.upper_bound.toFixed(1)}]</>
                      )}
                    </div>
                  </td>
                  <td><span className={`status ${p.severity === "critical" ? "alerting" : "warning"}`}>{p.severity}</span></td>
                  <td>{p.message}</td>
                  <td style={{ minWidth: "320px" }}>
                    {/* Faz 17 Ek İŞ B: rapor ve dashboard ile aynı öneri yapısı. */}
                    {p.advice ? (
                      <AdviceCard advice={p.advice} defaultOpen={false} />
                    ) : p.recommendation ? (
                      <>
                        <RecommendationHeader title={p.recommendation} />
                        {p.action && <CopyableAction command={p.action} />}
                        <PredictionPlaybook steps={p.playbook} />
                      </>
                    ) : (
                      <span className="muted-note">—</span>
                    )}
                  </td>
                  <td>
                    {canWrite && (
                      <button
                        className="btn"
                        onClick={() => {
                          // `catch` yoktu: kabul reddedilirse dugme hicbir sey yapmiyormus gibi
                          // gorunuyordu (Faz 19 IS 2).
                          setError(null);
                          api.ackPrediction(p.id).then(load).catch((e) => setError(errorMessage(e)));
                        }}
                      >
                        Onayla
                      </button>
                    )}
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
