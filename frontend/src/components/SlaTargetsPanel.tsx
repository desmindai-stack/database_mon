import { useEffect, useState } from "react";

import { api, type SlaTarget } from "../api";
import { PageError, PageLoading } from "./PageState";

/**
 * SLA hedefleri ve güncel durum (Faz 28 İŞ 3b).
 *
 * Hedef olmadan erişilebilirlik sayısı bir bilgi ama bir KARAR değil: %99.7 iyi mi kötü mü,
 * ancak taahhüde göre söylenebilir.
 *
 * Tabloda çıplak yüzdenin yanında iki türev sayı var ve asıl kararı onlar veriyor:
 * "en iyi durum" (kalan dönem kesintisiz geçerse ulaşılabilecek oran) ve "kalan bütçe"
 * (SLA'yı ihlal etmeden karşılanabilecek azami kesinti). Ayın 3'ünde "%99.2" görmek hiçbir
 * şey söylemez; "47 dakikanız kaldı" ise doğrudan bakım planlamak için kullanılabilir.
 */

const PERIOD_LABELS: Record<string, string> = {
  monthly: "Aylık",
  quarterly: "Çeyreklik",
};

const SCOPE_LABELS: Record<string, string> = {
  instance: "Veritabanı",
  group: "Veritabanı grubu",
  application: "Uygulama",
  customer: "Müşteri",
  global: "Tüm sistem",
};

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const negative = seconds < 0;
  const value = Math.abs(seconds);
  let text: string;
  if (value < 90) text = `${Math.round(value)} sn`;
  else if (value < 5400) text = `${Math.round(value / 60)} dk`;
  else if (value < 172800) text = `${(value / 3600).toFixed(1)} sa`;
  else text = `${(value / 86400).toFixed(1)} gün`;
  return negative ? `-${text}` : text;
}

export default function SlaTargetsPanel({ canWrite }: { canWrite: boolean }) {
  const [targets, setTargets] = useState<SlaTarget[] | null>(null);
  const [statuses, setStatuses] = useState<Record<string, any>[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [scopeType, setScopeType] = useState("customer");
  const [scopeId, setScopeId] = useState("");
  const [targetPct, setTargetPct] = useState("99.9");
  const [period, setPeriod] = useState("monthly");

  async function load() {
    try {
      const [rows, status] = await Promise.all([api.listSlaTargets(), api.getSlaStatus()]);
      setTargets(rows);
      setStatuses(status);
      setError(null);
    } catch (err) {
      setError(err);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      await api.createSlaTarget({
        scope_type: scopeType,
        scope_id: scopeType === "global" ? null : Number(scopeId) || null,
        target_pct: Number(targetPct),
        period,
      });
      setScopeId("");
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Kaydedilemedi.");
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    try {
      await api.deleteSlaTarget(id);
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Silinemedi.");
    }
  }

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h3 className="chart-title">SLA hedefleri</h3>
      <p className="muted-note">
        Erişilebilirlik yüzdesi yalnızca <strong>plansız</strong> kesintiye göre hesaplanır;
        tanımlı bakım penceresine denk gelen süre hedefi etkilemez. Ölçüm veritabanı
        erişilebilirliğidir — replikalı bir kümede bir düğümün düşmesi uygulama için kesinti
        olmayabilir.
      </p>

      {error != null && <PageError error={error} onRetry={load} />}
      {targets === null && error == null && <PageLoading />}
      {targets !== null && error == null && (
        <>
          {canWrite && (
            <form className="form-grid" onSubmit={submit} style={{ marginBottom: "1rem" }}>
              <label>
                Kapsam
                <select value={scopeType} onChange={(e) => setScopeType(e.target.value)}>
                  {Object.entries(SCOPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              {scopeType !== "global" && (
                <label>
                  Kapsam kimliği
                  <input
                    type="number"
                    value={scopeId}
                    onChange={(e) => setScopeId(e.target.value)}
                    placeholder="örn. 3"
                  />
                </label>
              )}
              <label>
                Hedef (%)
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="100"
                  value={targetPct}
                  onChange={(e) => setTargetPct(e.target.value)}
                />
              </label>
              <label>
                Ölçüm dönemi
                <select value={period} onChange={(e) => setPeriod(e.target.value)}>
                  {Object.entries(PERIOD_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <div style={{ gridColumn: "1 / -1" }}>
                <button type="submit" className="btn btn-primary" disabled={saving}>
                  {saving ? "Kaydediliyor…" : "SLA hedefi ekle"}
                </button>
                {formError && (
                  <span className="warn-text" style={{ marginLeft: "0.75rem" }}>{formError}</span>
                )}
              </div>
            </form>
          )}

          {statuses.length === 0 ? (
            <p className="muted-note">
              Tanımlı SLA hedefi yok — erişilebilirlik ölçülüyor ama hedefe uygunluk
              değerlendirilemiyor.
            </p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Kapsam</th>
                    <th>Dönem</th>
                    <th>Hedef</th>
                    <th>Gerçekleşen</th>
                    <th>En iyi durum</th>
                    <th>Planlı bakım</th>
                    <th>Plansız kesinti</th>
                    <th>Kalan bütçe</th>
                    {canWrite && <th />}
                  </tr>
                </thead>
                <tbody>
                  {statuses.map((row, index) => {
                    const target = (targets || []).find(
                      (t) => t.scope_type === row.scope_type && t.scope_id === row.scope_id,
                    );
                    return (
                      <tr key={`${row.scope_type}-${row.scope_id ?? "g"}-${index}`}>
                        <td>
                          {row.scope_label}
                          <div className="muted-note">
                            {SCOPE_LABELS[row.scope_type] || row.scope_type}
                          </div>
                        </td>
                        <td>{row.period_label}</td>
                        <td>%{row.target_pct}</td>
                        <td>
                          {row.measured ? (
                            <span className={row.met ? "ok-text" : "warn-text"}>
                              %{row.achieved_pct}
                            </span>
                          ) : (
                            <span className="muted-note">ölçülemedi</span>
                          )}
                        </td>
                        <td>
                          {row.measured ? (
                            <span className={row.already_lost ? "warn-text" : undefined}>
                              %{row.best_case_pct}
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td>{fmtDuration(row.planned_seconds)}</td>
                        <td>{fmtDuration(row.unplanned_seconds)}</td>
                        <td>
                          {row.measured ? (
                            <span
                              className={
                                (row.remaining_budget_seconds ?? 0) <= 0 ? "warn-text" : undefined
                              }
                            >
                              {fmtDuration(row.remaining_budget_seconds)}
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        {canWrite && (
                          <td>
                            {target && (
                              <button type="button" className="btn btn-xs" onClick={() => remove(target.id)}>
                                Sil
                              </button>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {statuses.some((r) => !r.measured && r.unknown_reason) && (
            <p className="muted-note">
              Ölçülemeyen kapsamlar: {statuses.filter((r) => !r.measured).map((r) => r.scope_label).join(", ")}.
              Ölçüm olmaması, sistemin ayakta olduğu anlamına gelmez.
            </p>
          )}
        </>
      )}
    </div>
  );
}
