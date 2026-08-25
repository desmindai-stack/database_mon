import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertEvent,
  AlertRule,
  AlertRuleCreate,
  AlertRuleType,
  api,
  DatabaseGroup,
  formatTime,
  Instance,
} from "../api";

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

export default function AlertsPage() {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [instances, setInstances] = useState<Instance[]>([]);
  const [groups, setGroups] = useState<DatabaseGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [form, setForm] = useState<AlertRuleCreate>(emptyForm());
  const [targetKind, setTargetKind] = useState<TargetKind>("instance");
  const [targetId, setTargetId] = useState<string>("");

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editThreshold, setEditThreshold] = useState<number>(0);
  const [editEnabled, setEditEnabled] = useState<boolean>(true);
  const [editSql, setEditSql] = useState<string>("");
  const [editIntervalSeconds, setEditIntervalSeconds] = useState<number>(60);
  const [editSeverity, setEditSeverity] = useState<string>("warning");

  const load = async () => {
    const [r, e, i, g] = await Promise.all([
      api.getAlertRules(),
      api.getAlertEvents(),
      api.getInstances(),
      api.getGroups(),
    ]);
    setRules(r);
    setEvents(e);
    setInstances(i);
    setGroups(g);
  };

  useEffect(() => {
    load().catch((err) => setError(String(err.message || err)));
  }, []);

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
      setForm(emptyForm());
      setTargetId("");
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (rule: AlertRule) => {
    setEditingId(rule.id);
    setEditThreshold(rule.threshold);
    setEditEnabled(rule.enabled);
    setEditSql(rule.sql_query || "");
    setEditIntervalSeconds(rule.interval_seconds);
    setEditSeverity(rule.severity);
  };

  const saveEdit = async (rule: AlertRule) => {
    const updates = rule.is_default
      ? { threshold: editThreshold, enabled: editEnabled }
      : {
          threshold: editThreshold,
          enabled: editEnabled,
          severity: editSeverity,
          interval_seconds: rule.rule_type === "custom" ? editIntervalSeconds : undefined,
          sql_query: rule.rule_type === "custom" ? editSql : undefined,
        };
    try {
      await api.updateAlertRule(rule.id, updates);
      setEditingId(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const targetLabel = (rule: AlertRule) => {
    if (rule.instance_id) {
      const inst = instances.find((i) => i.id === rule.instance_id);
      return inst ? `Instance: ${inst.name}` : `Instance #${rule.instance_id}`;
    }
    if (rule.group_id) {
      const grp = groups.find((g) => g.id === rule.group_id);
      return grp ? `Grup: ${grp.name}` : `Grup #${rule.group_id}`;
    }
    return "Tümü";
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Alerts</h2>
          <p>Varsayılan (otomatik) ve özel (SQL tabanlı) alarm kuralları</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3 style={{ color: "var(--text)", marginBottom: "1rem" }}>Özel kural ekle</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Ad
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
            </label>
            <label>
              Kural tipi
              <select
                value={form.rule_type}
                onChange={(e) => setForm({ ...form, rule_type: e.target.value as AlertRuleType })}
              >
                <option value="metric">Metrik (toplanan verilerden)</option>
                <option value="custom">Özel SQL sorgusu</option>
              </select>
            </label>
            <label>
              Hedef
              <select value={targetKind} onChange={(e) => { setTargetKind(e.target.value as TargetKind); setTargetId(""); }}>
                <option value="instance">Instance</option>
                <option value="group">Database Group</option>
              </select>
            </label>
            <label>
              {targetKind === "instance" ? "Instance" : "Grup"}
              <select value={targetId} onChange={(e) => setTargetId(e.target.value)} required={form.rule_type === "custom"}>
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
                <label>
                  SQL sorgusu (tek satır/tek kolon, salt-okunur)
                  <textarea
                    value={form.sql_query ?? ""}
                    onChange={(e) => setForm({ ...form, sql_query: e.target.value })}
                    placeholder="SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"
                    rows={3}
                    required
                  />
                </label>
                <p className="muted-note">
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
            <button type="submit" className="btn btn-primary" disabled={busy}>Kural ekle</button>
          </form>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Kural</th>
                <th>Tip</th>
                <th>Koşul</th>
                <th>Hedef</th>
                <th>Durum</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rules.length === 0 ? (
                <tr><td colSpan={6} className="empty">No alert rules</td></tr>
              ) : (
                rules.map((rule) => (
                  <tr key={rule.id}>
                    <td>
                      {rule.name}
                      {rule.is_default && <span className="tag" style={{ marginLeft: "0.4rem" }}>Varsayılan</span>}
                    </td>
                    <td>{rule.rule_type === "custom" ? "Özel SQL" : "Metrik"}</td>
                    <td>
                      {editingId === rule.id ? (
                        <input
                          type="number"
                          style={{ width: "5rem" }}
                          value={editThreshold}
                          onChange={(e) => setEditThreshold(Number(e.target.value))}
                        />
                      ) : (
                        <code>
                          {rule.rule_type === "custom" ? "sorgu sonucu" : rule.metric} {rule.operator} {rule.threshold}
                        </code>
                      )}
                    </td>
                    <td>{targetLabel(rule)}</td>
                    <td>
                      {editingId === rule.id ? (
                        <label>
                          <input type="checkbox" checked={editEnabled} onChange={(e) => setEditEnabled(e.target.checked)} /> aktif
                        </label>
                      ) : (
                        <span className={`status ${rule.enabled ? "healthy" : "disabled"}`}>{rule.enabled ? "aktif" : "kapalı"}</span>
                      )}
                    </td>
                    <td>
                      {editingId === rule.id ? (
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          <button className="btn btn-primary" onClick={() => saveEdit(rule)}>Kaydet</button>
                          <button className="btn" onClick={() => setEditingId(null)}>Vazgeç</button>
                        </div>
                      ) : (
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          <button className="btn" onClick={() => startEdit(rule)}>Düzenle</button>
                          {!rule.is_default && (
                            <button className="btn btn-danger" onClick={() => api.deleteAlertRule(rule.id).then(load)}>
                              Sil
                            </button>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>Son tetiklenen olaylar</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Scope</th>
                <th>Message</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {events.length === 0 ? (
                <tr><td colSpan={4} className="empty">No active alerts</td></tr>
              ) : (
                events.map((event) => (
                  <tr key={event.id}>
                    <td>{formatTime(event.triggered_at)}</td>
                    <td>
                      {event.instance_id ? (
                        `Instance #${event.instance_id}`
                      ) : event.group_id ? (
                        <Link to={`/groups/${event.group_id}`}>Group #{event.group_id}</Link>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{event.message}</td>
                    <td>
                      <button className="btn" onClick={() => api.resolveAlert(event.id).then(load)}>
                        Resolve
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
