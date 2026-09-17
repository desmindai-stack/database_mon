// Faz 31 İŞ 2 — bir sorgunun planı nereden alınabilir.
//
// Dört kaynak HER ZAMAN, öncelik sırasıyla gösteriliyor; kullanılamayan kaynak gizlenmiyor,
// sebebiyle listeleniyor. Kaynakların güvenilirliği farklı — kullanıcı hangisine baktığını
// her zaman görmeli (ExplainPlanTree de alınan planın kaynağını etiketliyor).
import { useState } from "react";
import { api, ExplainResult, PlanSources, SlowQuery } from "../api";
import { useAuth } from "../auth";
import ExplainPlanTree from "./ExplainPlanTree";

type Props = { instanceId: number; query: SlowQuery };

// Faz 31 Commit 5: "yakalanan plan yok" üç ayrı durumdu ve aynı metinle gösteriliyordu.
const CAPTURE_UNAVAILABLE_LABELS: Record<string, string> = {
  not_measured: "Ölçülemedi",
  disabled_on_target: "Hedefte kapalı",
  no_plans_yet: "Henüz plan yok",
  no_agent: "Agent yok",
  not_postgresql: "Desteklenmiyor",
};

export default function PlanSourcePanel({ instanceId, query }: Props) {
  const canWrite = useAuth().user?.role === "admin";
  const [sources, setSources] = useState<PlanSources | null>(null);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [plan, setPlan] = useState<ExplainResult | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const loadSources = async () => {
    setBusy("sources");
    setSourcesError(null);
    try {
      setSources(await api.getPlanSources(instanceId, query.id));
    } catch (e) {
      setSources(null);
      setSourcesError(String((e as Error).message || e));
    } finally {
      setBusy(null);
    }
  };

  const run = async (kind: string, action: () => Promise<ExplainResult>) => {
    setBusy(kind);
    setPlanError(null);
    try {
      setPlan(await action());
    } catch (e) {
      setPlan(null);
      setPlanError(String((e as Error).message || e));
    } finally {
      setBusy(null);
    }
  };

  if (!sources) {
    return (
      <div className="plan-sources">
        <button className="btn" onClick={loadSources} disabled={busy === "sources"}>
          {busy === "sources" ? "Plan kaynakları yükleniyor…" : "Plan"}
        </button>
        {sourcesError && <p className="advice-empty">Plan kaynakları alınamadı: {sourcesError}</p>}
      </div>
    );
  }

  return (
    <div className="plan-sources">
      <ol className="plan-source-list">
        {sources.options.map((option) => {
          const recommended = option.kind === sources.recommended && option.available;
          return (
            <li key={option.kind} className={`plan-source ${option.available ? "available" : "unavailable"}`}>
              <div className="plan-source-head">
                <strong>{option.label}</strong>
                <span className="advice-pill">
                  {option.available ? (recommended ? "Önerilen" : "Kullanılabilir") : "Kullanılamıyor"}
                </span>
                {!option.available && option.kind === "captured" && option.detail?.unavailable_kind != null && (
                  <span className="tag warn">
                    {CAPTURE_UNAVAILABLE_LABELS[String(option.detail.unavailable_kind)] ??
                      String(option.detail.unavailable_kind)}
                  </span>
                )}
              </div>
              {option.available ? (
                option.caveat && <p className="muted-note">{option.caveat}</p>
              ) : (
                option.kind !== "unavailable" && <p className="muted-note">{option.reason}</p>
              )}
              {option.kind === "unavailable" && option.available && <p className="advice-empty">{option.reason}</p>}
              {option.available && option.kind === "captured" && (
                <button
                  className="btn btn-primary"
                  disabled={busy !== null}
                  onClick={() =>
                    run("captured", () => api.getCapturedPlan(instanceId, Number(option.detail?.plan_id)))
                  }
                >
                  {busy === "captured" ? "Yükleniyor…" : "Yakalanan planı göster"}
                </button>
              )}
              {option.available && option.kind === "sample_analyze" && canWrite && (
                <button
                  className="btn btn-danger"
                  disabled={busy !== null}
                  onClick={() => {
                    if (confirm(`Gerçek değerlerle EXPLAIN ANALYZE\n\n${option.caveat ?? ""}\n\nDevam edilsin mi?`)) {
                      void run("sample_analyze", () => api.explainWithSample(instanceId, query.query, query.id));
                    }
                  }}
                >
                  {busy === "sample_analyze" ? "Çalıştırılıyor…" : "Gerçek değerlerle EXPLAIN ANALYZE ⚠"}
                </button>
              )}
              {option.available && option.kind === "sample_analyze" && !canWrite && (
                <p className="muted-note">Sorguyu çalıştıran bu seçenek yalnızca yöneticiye açık.</p>
              )}
              {option.available && option.kind === "generic" && (
                <button
                  className="btn"
                  disabled={busy !== null}
                  onClick={() => run("generic", () => api.explainQuery(instanceId, query.query, false))}
                >
                  {busy === "generic" ? "Plan alınıyor…" : "Planı göster (çalıştırmadan)"}
                </button>
              )}
            </li>
          );
        })}
      </ol>
      {planError && <p className="advice-empty">Plan alınamadı: {planError}</p>}
      {plan && <ExplainPlanTree result={plan} />}
    </div>
  );
}
