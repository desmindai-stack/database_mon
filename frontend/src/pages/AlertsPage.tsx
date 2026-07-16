import { FormEvent, useEffect, useState } from "react";
import { api, AlertEvent, AlertRule, formatTime, Instance } from "../api";

const METRICS = [
  "active_connections",
  "cache_hit_ratio",
  "transactions_per_sec",
  "replication_lag_bytes",
  "database_size_bytes",
  "deadlocks",
  "temp_bytes",
];

export default function AlertsPage() {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [instances, setInstances] = useState<Instance[]>([]);
  const [form, setForm] = useState({
    name: "",
    metric: "active_connections",
    operator: ">",
    threshold: 80,
    instance_id: "" as string | number,
    enabled: true,
  });

  const load = async () => {
    const [r, e, i] = await Promise.all([
      api.getAlertRules(),
      api.getAlertEvents(),
      api.getInstances(),
    ]);
    setRules(r);
    setEvents(e);
    setInstances(i);
  };

  useEffect(() => {
    load().catch(() => undefined);
  }, []);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    await api.createAlertRule({
      name: form.name,
      metric: form.metric,
      operator: form.operator,
      threshold: Number(form.threshold),
      instance_id: form.instance_id === "" ? null : Number(form.instance_id),
      enabled: form.enabled,
    });
    setForm({ ...form, name: "" });
    await load();
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Alerts</h2>
          <p>Threshold-based alerting on collected metrics</p>
        </div>
      </header>

      <div className="grid grid-2">
        <div className="card">
          <h3 style={{ color: "var(--text)", marginBottom: "1rem" }}>Create rule</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Name
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
            </label>
            <label>
              Instance (optional)
              <select value={form.instance_id} onChange={(e) => setForm({ ...form, instance_id: e.target.value })}>
                <option value="">All instances</option>
                {instances.map((i) => (
                  <option key={i.id} value={i.id}>{i.name}</option>
                ))}
              </select>
            </label>
            <label>
              Metric
              <select value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })}>
                {METRICS.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label>
              Operator
              <select value={form.operator} onChange={(e) => setForm({ ...form, operator: e.target.value })}>
                {["<", "<=", ">", ">=", "=="].map((op) => <option key={op} value={op}>{op}</option>)}
              </select>
            </label>
            <label>
              Threshold
              <input type="number" value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />
            </label>
            <button type="submit" className="btn btn-primary">Create rule</button>
          </form>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rule</th>
                <th>Condition</th>
                <th>Scope</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rules.length === 0 ? (
                <tr><td colSpan={4} className="empty">No alert rules</td></tr>
              ) : (
                rules.map((rule) => (
                  <tr key={rule.id}>
                    <td>{rule.name}</td>
                    <td><code>{rule.metric} {rule.operator} {rule.threshold}</code></td>
                    <td>{rule.instance_id ? `#${rule.instance_id}` : "All"}</td>
                    <td>
                      <button className="btn btn-danger" onClick={() => api.deleteAlertRule(rule.id).then(load)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <h3 style={{ color: "var(--text)", marginBottom: "0.75rem" }}>Active alerts</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Instance</th>
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
                    <td>#{event.instance_id}</td>
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
