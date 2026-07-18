import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, DbEngine, ENGINE_DEFAULTS, Instance, InstanceCreate } from "../api";

const emptyForm = (engine: DbEngine = "postgresql"): InstanceCreate => ({
  name: "",
  engine,
  host: "localhost",
  port: ENGINE_DEFAULTS[engine].port,
  database: ENGINE_DEFAULTS[engine].database,
  username: engine === "mongodb" ? "admin" : engine === "sqlserver" ? "sa" : "postgres",
  password: "",
});

export default function InstancesPage() {
  const [instances, setInstances] = useState<Instance[]>([]);
  const [form, setForm] = useState<InstanceCreate>(emptyForm());
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.getInstances().then(setInstances).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    load();
  }, []);

  const setEngine = (engine: DbEngine) => {
    setForm(emptyForm(engine));
    setTestResult(null);
  };

  const update = (key: keyof InstanceCreate, value: string | number) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const onTest = async () => {
    setBusy(true);
    setTestResult(null);
    try {
      const result = await api.testConnection(form);
      setTestResult(result.ok ? `OK — ${String(result.details.version ?? result.message)}` : result.message);
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
      setForm(emptyForm(form.engine));
      setTestResult(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (id: number) => {
    if (!confirm("Instance ve tüm metrikleri silinsin mi?")) return;
    await api.deleteInstance(id);
    await load();
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Instances</h2>
          <p>PostgreSQL, SQL Server ve MongoDB sunucularını kaydedin</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni instance</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Motor
              <select value={form.engine} onChange={(e) => setEngine(e.target.value as DbEngine)}>
                <option value="postgresql">PostgreSQL</option>
                <option value="sqlserver">SQL Server</option>
                <option value="mongodb">MongoDB</option>
              </select>
            </label>
            <label>
              Ad
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
              Kullanıcı
              <input value={form.username} onChange={(e) => update("username", e.target.value)} required />
            </label>
            <label>
              Şifre
              <input type="password" value={form.password} onChange={(e) => update("password", e.target.value)} required />
            </label>
            {testResult && (
              <div style={{ color: testResult.startsWith("OK") ? "var(--success)" : "var(--danger)" }}>
                {testResult}
              </div>
            )}
            <div className="form-actions">
              <button type="button" className="btn" onClick={onTest} disabled={busy}>
                Bağlantı testi
              </button>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                Ekle
              </button>
            </div>
          </form>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ad</th>
                <th>Motor</th>
                <th>Hedef</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {instances.length === 0 ? (
                <tr><td colSpan={4} className="empty">Kayıtlı instance yok</td></tr>
              ) : (
                instances.map((inst) => (
                  <tr key={inst.id}>
                    <td><Link to={`/instances/${inst.id}`}>{inst.name}</Link></td>
                    <td>{inst.engine}</td>
                    <td>{inst.host}:{inst.port}/{inst.database}</td>
                    <td><button className="btn btn-danger" onClick={() => onDelete(inst.id)}>Sil</button></td>
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
