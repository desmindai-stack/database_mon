import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertRuleCreate, AlertRuleType, api, ConnectionTestResult, DatabaseGroup, Instance } from "../api";
import { useAuth } from "../auth";
import { NotFoundState } from "../components/PageState";

const METRICS = [
  "active_connections",
  "connection_utilization_pct",
  "cache_hit_ratio",
  "transactions_per_sec",
  "ops_per_sec",
  "replication_lag_bytes",
  "database_size_bytes",
  "deadlocks",
  "temp_bytes",
];

const OPERATORS = ["<", "<=", ">", ">=", "=="];
const SEVERITIES = ["critical", "high", "warning", "medium", "low", "info"];
const ENGINES = ["postgresql", "sqlserver", "mongodb"];

type TargetKind = "instance" | "group";

const emptyForm = (): AlertRuleCreate => ({
  name: "",
  rule_type: "metric",
  metric: "active_connections",
  operator: ">",
  threshold: 80,
  enabled: true,
  severity: "warning",
  engine: "postgresql",
  sql_query: "",
  interval_seconds: 60,
});

// Full-page form (İŞ 7) — replaces the old inline "Özel kural ekle" card so the SQL editor has
// real room to breathe instead of fighting a half-width sidebar column.
export default function CustomAlertRuleFormPage() {
  const canWrite = useAuth().user?.role === "admin";
  const navigate = useNavigate();
  const [instances, setInstances] = useState<Instance[]>([]);
  const [groups, setGroups] = useState<DatabaseGroup[]>([]);
  const [form, setForm] = useState<AlertRuleCreate>(emptyForm());
  const [targetKind, setTargetKind] = useState<TargetKind>("instance");
  const [targetId, setTargetId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    Promise.all([api.getInstances(), api.getGroups()])
      .then(([i, g]) => {
        setInstances(i);
        setGroups(g);
      })
      .catch(() => undefined);
  }, []);

  const onTestQuery = async () => {
    if (!form.sql_query?.trim()) {
      setTestResult({ ok: false, message: "Önce bir sorgu girin", details: {} });
      return;
    }
    if (!targetId) {
      setTestResult({ ok: false, message: "Önce bir hedef seçin", details: {} });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.testAlertRuleQuery({
        sql_query: form.sql_query,
        instance_id: targetKind === "instance" ? Number(targetId) : undefined,
        group_id: targetKind === "group" ? Number(targetId) : undefined,
      });
      setTestResult(result);
    } catch (err) {
      setTestResult({ ok: false, message: String((err as Error).message), details: {} });
    } finally {
      setTesting(false);
    }
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const payload: AlertRuleCreate = {
        ...form,
        instance_id: targetKind === "instance" && targetId ? Number(targetId) : undefined,
        group_id: targetKind === "group" && targetId ? Number(targetId) : undefined,
      };
      if (form.rule_type === "metric") {
        payload.engine = undefined;
        payload.sql_query = undefined;
      } else {
        payload.metric = undefined;
      }
      await api.createAlertRule(payload);
      navigate("/alerts");
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  if (!canWrite) {
    // Ciplak hata kutusu yerine geri donus yolu olan ortak ekran (Faz 19 IS 3).
    return (
      <NotFoundState
        title="Bu sayfa için yetkiniz yok"
        detail="Alarm kuralı eklemek admin yetkisi gerektirir; viewer rolü salt-okunurdur."
        backTo="/alerts"
        backLabel="Alarmlara dön"
      />
    );
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Özel kural ekle</h2>
          <p><Link to="/alerts">← Alerts</Link></p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="card" style={{ maxWidth: 760 }}>
        <form className="form-grid" onSubmit={onSubmit}>
          <label>
            Ad
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          </label>
          <label>
            Kural tipi
            <select
              value={form.rule_type}
              onChange={(e) => { setForm({ ...form, rule_type: e.target.value as AlertRuleType }); setTestResult(null); }}
            >
              <option value="metric">Metrik (toplanan verilerden)</option>
              <option value="custom">Özel SQL sorgusu</option>
            </select>
          </label>
          <label>
            Hedef
            <select
              value={targetKind}
              onChange={(e) => { setTargetKind(e.target.value as TargetKind); setTargetId(""); setTestResult(null); }}
            >
              <option value="instance">Instance</option>
              <option value="group">Database Group</option>
            </select>
          </label>
          <label>
            {targetKind === "instance" ? "Instance" : "Grup"}
            <select
              value={targetId}
              onChange={(e) => { setTargetId(e.target.value); setTestResult(null); }}
              required={form.rule_type === "custom"}
            >
              <option value="">
                {form.rule_type === "custom" ? "— seçin —" : targetKind === "instance" ? "Tüm instance'lar" : "— seçin —"}
              </option>
              {(targetKind === "instance" ? instances : groups).map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </label>

          {form.rule_type === "metric" ? (
            <label>
              Metrik
              <select value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })}>
                {METRICS.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
          ) : (
            <>
              <label>
                Engine
                <select value={form.engine ?? "postgresql"} onChange={(e) => setForm({ ...form, engine: e.target.value })}>
                  {ENGINES.map((eng) => <option key={eng} value={eng}>{eng}</option>)}
                </select>
              </label>
              <label>
                Çalışma aralığı (sn)
                <input
                  type="number"
                  min={10}
                  max={3600}
                  value={form.interval_seconds}
                  onChange={(e) => setForm({ ...form, interval_seconds: Number(e.target.value) })}
                />
              </label>
              <label style={{ gridColumn: "1 / -1" }}>
                SQL sorgusu (tek satır/tek kolon, salt-okunur)
                <textarea
                  value={form.sql_query ?? ""}
                  onChange={(e) => { setForm({ ...form, sql_query: e.target.value }); setTestResult(null); }}
                  placeholder="SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"
                  rows={10}
                  className="sql-editor"
                  required
                />
              </label>
              <div style={{ gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
                <button type="button" className="btn" disabled={testing} onClick={onTestQuery}>
                  {testing ? "Test ediliyor…" : "Sorguyu test et"}
                </button>
                {testResult && (
                  <span className={testResult.ok ? "ok-text" : "warn-text"}>{testResult.message}</span>
                )}
              </div>
              <p className="muted-note" style={{ gridColumn: "1 / -1" }}>
                Sadece SELECT/WITH ile başlayan sorgulara izin verilir; DDL/DML reddedilir, 10 saniye zaman
                aşımı ve salt-okunur bağlantı uygulanır.
              </p>
            </>
          )}

          <label>
            Operatör
            <select value={form.operator} onChange={(e) => setForm({ ...form, operator: e.target.value })}>
              {OPERATORS.map((op) => <option key={op} value={op}>{op}</option>)}
            </select>
          </label>
          <label>
            Eşik değeri
            <input type="number" value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />
          </label>
          <label>
            Önem derecesi
            <select value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value })}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <div className="form-actions" style={{ gridColumn: "1 / -1" }}>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Ekleniyor…" : "Kural ekle"}
            </button>
            <Link to="/alerts" className="btn">İptal</Link>
          </div>
        </form>
      </div>
    </>
  );
}
