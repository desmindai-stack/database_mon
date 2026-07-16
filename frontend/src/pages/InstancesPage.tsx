import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Instance, InstanceCreate } from "../api";

const emptyForm: InstanceCreate = {
  name: "",
  host: "localhost",
  port: 5432,
  database: "postgres",
  username: "postgres",
  password: "",
};

export default function InstancesPage() {
  const [instances, setInstances] = useState<Instance[]>([]);
  const [form, setForm] = useState<InstanceCreate>(emptyForm);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.getInstances().then(setInstances).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    load();
  }, []);

  const update = (key: keyof InstanceCreate, value: string | number) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const onTest = async () => {
    setBusy(true);
    setTestResult(null);
    try {
      const result = await api.testConnection(form);
      setTestResult(result.ok ? `OK — ${result.details.version ?? result.message}` : result.message);
    } catch (e) {
      setTestResult(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createInstance(form);
      setForm(emptyForm);
      setTestResult(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (id: number) => {
    if (!confirm("Delete this instance and all its metrics?")) return;
    await api.deleteInstance(id);
    await load();
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Instances</h2>
          <p>Register PostgreSQL servers to monitor</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Add instance</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Name
              <input value={form.name} onChange={(e) => update("name", e.target.value)} required />
            </label>
            <label>
              Host
              <input value={form.host} onChange={(e) => update("host", e.target.value)} required />
            </label>
            <label>
              Port
              <input type="number" value={form.port} onChange={(e) => update("port", Number(e.target.value))} />
            </label>
            <label>
              Database
              <input value={form.database} onChange={(e) => update("database", e.target.value)} required />
            </label>
            <label>
              Username
              <input value={form.username} onChange={(e) => update("username", e.target.value)} required />
            </label>
            <label>
              Password
              <input type="password" value={form.password} onChange={(e) => update("password", e.target.value)} required />
            </label>
            {testResult && <div style={{ color: testResult.startsWith("OK") ? "var(--success)" : "var(--danger)" }}>{testResult}</div>}
            <div className="form-actions">
              <button type="button" className="btn" onClick={onTest} disabled={busy}>Test connection</button>
              <button type="submit" className="btn btn-primary" disabled={busy}>Add instance</button>
            </div>
          </form>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Target</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {instances.length === 0 ? (
                <tr><td colSpan={4} className="empty">No instances registered</td></tr>
              ) : (
                instances.map((inst) => (
                  <tr key={inst.id}>
                    <td><Link to={`/instances/${inst.id}`}>{inst.name}</Link></td>
                    <td>{inst.host}:{inst.port}/{inst.database}</td>
                    <td><span className={`status ${inst.enabled ? "healthy" : "disabled"}`}>{inst.enabled ? "enabled" : "disabled"}</span></td>
                    <td><button className="btn btn-danger" onClick={() => onDelete(inst.id)}>Delete</button></td>
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
