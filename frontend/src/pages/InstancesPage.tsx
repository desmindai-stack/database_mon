import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  ClusterServiceOptions,
  DbEngine,
  ENGINE_DEFAULTS,
  HealthResponse,
  Instance,
  InstanceCreate,
} from "../api";

const PG_SERVICES = ["etcd", "patroni", "postgresql", "keepalived", "haproxy"];

const defaultOptions = (): ClusterServiceOptions => ({
  patroni_port: 8008,
  etcd_port: 2379,
  haproxy_stats_port: 8404,
  haproxy_stats_path: "/stats;csv",
  keepalived_vip: "",
  probe_timeout_sec: 3,
  patroni_tls: false,
  agent_url: "",
  agent_token: "",
});

const emptyForm = (engine: DbEngine = "postgresql", defaultCustomer?: string): InstanceCreate => ({
  name: "",
  engine,
  host: "localhost",
  port: ENGINE_DEFAULTS[engine].port,
  database: ENGINE_DEFAULTS[engine].database,
  username: engine === "mongodb" ? "admin" : engine === "sqlserver" ? "sa" : "postgres",
  password: "",
  customer_name: defaultCustomer ?? "",
  environment: "public",
  application: "",
  cluster_name: "",
  role: "",
  services: [],
  options: defaultOptions(),
});

const instanceToForm = (inst: Instance): InstanceCreate => ({
  name: inst.name,
  engine: inst.engine,
  host: inst.host,
  port: inst.port,
  database: inst.database,
  username: inst.username,
  password: "",
  customer_name: inst.customer_name ?? "",
  environment: inst.environment,
  application: inst.application ?? "",
  cluster_name: inst.cluster_name ?? "",
  role: inst.role ?? "",
  services: inst.services ?? [],
  options: { ...defaultOptions(), ...(inst.options || {}) },
});

export default function InstancesPage() {
  const [instances, setInstances] = useState<Instance[]>([]);
  const [form, setForm] = useState<InstanceCreate>(emptyForm());
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [config, setConfig] = useState<HealthResponse | null>(null);
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);

  const isPrivate = config?.deployment_mode === "private";
  const defaultCustomer = config?.default_customer_name ?? undefined;

  const load = () => api.getInstances().then(setInstances).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    load();
    api.getHealth().then((c) => {
      setConfig(c);
      setForm((prev) => ({ ...prev, customer_name: c.default_customer_name ?? prev.customer_name }));
    }).catch(() => undefined);
  }, []);

  const setEngine = (engine: DbEngine) => {
    setForm((prev) => ({ ...prev, engine, port: ENGINE_DEFAULTS[engine].port, database: ENGINE_DEFAULTS[engine].database }));
  };

  const update = (key: keyof InstanceCreate, value: string | number | string[] | ClusterServiceOptions) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const updateOption = <K extends keyof ClusterServiceOptions>(key: K, value: ClusterServiceOptions[K]) => {
    setForm((prev) => ({
      ...prev,
      options: { ...defaultOptions(), ...(prev.options || {}), [key]: value },
    }));
  };

  const toggleService = (service: string) => {
    const current = form.services ?? [];
    const next = current.includes(service)
      ? current.filter((s) => s !== service)
      : [...current, service];
    update("services", next);
  };

  const startAdd = () => {
    setForm(emptyForm("postgresql", isPrivate ? defaultCustomer : undefined));
    setEditingId(null);
    setTestResult(null);
    setIsFormOpen(true);
  };

  const startEdit = (inst: Instance) => {
    setForm(instanceToForm(inst));
    setEditingId(inst.id);
    setTestResult(null);
    setIsFormOpen(true);
  };

  const cancelForm = () => {
    setIsFormOpen(false);
    setEditingId(null);
    setTestResult(null);
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
      if (editingId !== null) {
        await api.updateInstance(editingId, form);
      } else {
        await api.createInstance(form);
      }
      setForm(emptyForm(form.engine, isPrivate ? defaultCustomer : undefined));
      setTestResult(null);
      setIsFormOpen(false);
      setEditingId(null);
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

  const formPanel = (
    <div className="card">
      <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>
        {editingId !== null ? "Instance düzenle" : "Yeni instance parametreleri"}
      </h3>
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
        {!isPrivate && (
          <label>
            Müşteri
            <input value={form.customer_name} onChange={(e) => update("customer_name", e.target.value)} />
          </label>
        )}
        <label>
          Ortam
          <select value={form.environment} onChange={(e) => update("environment", e.target.value)}>
            <option value="public">Public</option>
            <option value="private">Private</option>
          </select>
        </label>
        <label>
          Uygulama
          <input value={form.application} onChange={(e) => update("application", e.target.value)} />
        </label>
        <label>
          Cluster
          <input value={form.cluster_name} onChange={(e) => update("cluster_name", e.target.value)} />
        </label>
        <label>
          Rol
          <input value={form.role} onChange={(e) => update("role", e.target.value)} placeholder="primary, replica, haproxy..." />
        </label>
        <label>
          Sunucu servisleri
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.35rem" }}>
            {PG_SERVICES.map((svc) => (
              <label key={svc} style={{ display: "flex", alignItems: "center", gap: "0.3rem", fontWeight: 400 }}>
                <input
                  type="checkbox"
                  checked={(form.services ?? []).includes(svc)}
                  onChange={() => toggleService(svc)}
                />
                {svc}
              </label>
            ))}
          </div>
        </label>
        {form.engine === "postgresql" && (
          <>
            <label>
              Patroni port
              <input
                type="number"
                value={form.options?.patroni_port ?? 8008}
                onChange={(e) => updateOption("patroni_port", Number(e.target.value))}
              />
            </label>
            <label>
              etcd port
              <input
                type="number"
                value={form.options?.etcd_port ?? 2379}
                onChange={(e) => updateOption("etcd_port", Number(e.target.value))}
              />
            </label>
            <label>
              HAProxy stats port
              <input
                type="number"
                value={form.options?.haproxy_stats_port ?? 8404}
                onChange={(e) => updateOption("haproxy_stats_port", Number(e.target.value))}
              />
            </label>
            <label>
              HAProxy stats path
              <input
                value={form.options?.haproxy_stats_path ?? "/stats;csv"}
                onChange={(e) => updateOption("haproxy_stats_path", e.target.value)}
              />
            </label>
            <label>
              Keepalived VIP
              <input
                value={form.options?.keepalived_vip ?? ""}
                onChange={(e) => updateOption("keepalived_vip", e.target.value)}
                placeholder="10.0.0.50"
              />
            </label>
            <label>
              Host agent URL
              <input
                value={form.options?.agent_url ?? ""}
                onChange={(e) => updateOption("agent_url", e.target.value)}
                placeholder="http://db-host:9105"
              />
            </label>
            <label>
              Host agent token
              <input
                type="password"
                value={form.options?.agent_token ?? ""}
                onChange={(e) => updateOption("agent_token", e.target.value)}
                placeholder="shared secret"
              />
            </label>
          </>
        )}
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
          <input type="password" value={form.password} onChange={(e) => update("password", e.target.value)} placeholder={editingId !== null ? "Değiştirmek için yazın" : ""} required={editingId === null} />
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
            {editingId !== null ? "Güncelle" : "Ekle"}
          </button>
          <button type="button" className="btn" onClick={cancelForm}>
            İptal
          </button>
        </div>
      </form>
    </div>
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Instances</h2>
          <p>PostgreSQL, SQL Server ve MongoDB sunucularını kaydedin</p>
        </div>
        <button className="btn btn-primary" onClick={startAdd}>
          + Yeni Instance
        </button>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ad</th>
                {!isPrivate && <th>Müşteri</th>}
                <th>Ortam</th>
                <th>Uygulama</th>
                <th>Cluster</th>
                <th>Rol</th>
                <th>Motor</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {instances.length === 0 ? (
                <tr><td colSpan={isPrivate ? 7 : 8} className="empty">Kayıtlı instance yok</td></tr>
              ) : (
                instances.map((inst) => (
                  <tr key={inst.id}>
                    <td><Link to={`/instances/${inst.id}`}>{inst.name}</Link></td>
                    {!isPrivate && <td>{inst.customer_name || "—"}</td>}
                    <td>{inst.environment}</td>
                    <td>{inst.application || "—"}</td>
                    <td>{inst.cluster_name || "—"}</td>
                    <td>{inst.role || "—"}</td>
                    <td>{inst.engine}</td>
                    <td>
                      <div style={{ display: "flex", gap: "0.4rem" }}>
                        <button className="btn" onClick={() => startEdit(inst)}>Düzenle</button>
                        <button className="btn btn-danger" onClick={() => onDelete(inst.id)}>Sil</button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {isFormOpen ? (
          formPanel
        ) : (
          <div className="card" style={{ display: "grid", placeItems: "center", minHeight: 200 }}>
            <p style={{ color: "var(--muted)", textAlign: "center" }}>
              Instance eklemek veya düzenlemek için<br />sol üstteki <strong>+ Yeni Instance</strong> veya listedeki <strong>Düzenle</strong> butonuna tıklayın.
            </p>
          </div>
        )}
      </div>
    </>
  );
}
