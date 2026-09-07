import { FormEvent, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  ClusterServiceOptions,
  DbEngine,
  ENGINE_DEFAULTS,
  HealthResponse,
  Instance,
  InstanceCreate,
  InstanceDependencies,
} from "../api";
import { useAuth } from "../auth";
import { showField, topologyOf, type FieldContext, type FormTopology } from "../formFields";
import { TableState } from "../components/PageState";
import { Pagination, usePagination } from "../components/Pagination";

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
  collect_interval_seconds: undefined,
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
  collect_interval_seconds: inst.collect_interval_seconds ?? undefined,
});

export default function InstancesPage() {
  const canWrite = useAuth().user?.role === "admin";
  const [instances, setInstances] = useState<Instance[]>([]);
  const [form, setForm] = useState<InstanceCreate>(emptyForm());
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [config, setConfig] = useState<HealthResponse | null>(null);
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  // Faz 16-B İŞ 2: silme onayı artık "emin misin?" değil, "şunlar da silinecek" diyor.
  const [deleteTarget, setDeleteTarget] = useState<Instance | null>(null);
  const [deleteDeps, setDeleteDeps] = useState<InstanceDependencies | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();

  const isPrivate = config?.deployment_mode === "private";
  const defaultCustomer = config?.default_customer_name ?? undefined;

  // `/api/instances` sınırsız döner; bir bankada birkaç yüz kayıt olması normal ve her satır
  // kendi düğmeleri/rozetleriyle geldiği için hepsini tek seferde render etmek ağır (Faz 19 İŞ 3).
  const instanceSlice = usePagination(instances);

  const load = () =>
    api
      .getInstances()
      .then((rows) => {
        setInstances(rows);
        setListError(null);
      })
      .catch(setListError)
      .finally(() => setLoaded(true));

  useEffect(() => {
    load();
    api.getHealth().then((c) => {
      setConfig(c);
      setForm((prev) => ({ ...prev, customer_name: c.default_customer_name ?? prev.customer_name }));
    }).catch(() => undefined);
  }, []);

  // Instance detay sayfasından "Bağlantı ayarlarını düzenle" ile gelindiğinde formu doğrudan aç
  // (Faz 16-B İŞ 2: kullanıcı düzenleme ekranını bulamıyordu).
  useEffect(() => {
    const editId = Number(searchParams.get("edit"));
    if (!editId || instances.length === 0) return;
    const target = instances.find((i) => i.id === editId);
    if (!target) return;
    setForm(instanceToForm(target));
    setTopologyState(topologyOf(target.cluster_name));
    setEditingId(target.id);
    setIsFormOpen(true);
    searchParams.delete("edit");
    setSearchParams(searchParams, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instances, searchParams]);

  // Topoloji formun kendi durumu: `Instance` modelinde ayrı bir kolon yok, cluster üyeliği
  // `cluster_name` ile işaretleniyor. Düzenlemeye açılan bir kayıt için ondan türetiliyor.
  const [topology, setTopologyState] = useState<FormTopology>("standalone");
  const fieldCtx: FieldContext = { engine: form.engine, topology };

  const setTopology = (next: FormTopology) => {
    setTopologyState(next);
    if (next === "standalone") {
      // Alanlar DOM'dan çıkıyor; taşıdıkları değerler de temizlenmeli, yoksa görünmeyen
      // bir cluster adı kaydedilip instance yanlış gruplanır.
      setForm((prev) => ({
        ...prev,
        cluster_name: "",
        role: "",
        services: [],
        options: { ...(prev.options || {}), keepalived_vip: "" },
      }));
    }
  };

  const setEngine = (engine: DbEngine) => {
    setForm((prev) => ({ ...prev, engine, port: ENGINE_DEFAULTS[engine].port, database: ENGINE_DEFAULTS[engine].database }));
    // MongoDB'de cluster topolojisi modellenmiyor (bkz. formFields.ts) — seçim standalone'a
    // düşürülüyor ki ilgisiz alanlar açık kalmasın.
    if (engine === "mongodb") setTopology("standalone");
  };

  const update = (key: keyof InstanceCreate, value: string | number | string[] | ClusterServiceOptions) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const updateCollectInterval = (raw: string) => {
    const value = raw.trim() === "" ? undefined : Number(raw);
    setForm((prev) => ({ ...prev, collect_interval_seconds: value }));
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

  const startEdit = (inst: Instance) => {
    setForm(instanceToForm(inst));
    // `Instance` modelinde topoloji kolonu yok; cluster üyeliği `cluster_name` ile işaretli.
    setTopologyState(topologyOf(inst.cluster_name));
    setEditingId(inst.id);
    setTestResult(null);
    setFieldErrors({});
    setIsFormOpen(true);
  };

  const cancelForm = () => {
    setIsFormOpen(false);
    setEditingId(null);
    setTopologyState("standalone");
    setTestResult(null);
    setFieldErrors({});
  };

  const onTest = async () => {
    setBusy(true);
    setTestResult(null);
    try {
      // Var olan bir instance düzenleniyorsa /test-config kullanılır: form şifreyi hiç
      // göstermediği için düz /instances/test boş şifreyle deneyip hep başarısız oluyordu.
      const result = editingId !== null
        ? await api.testInstanceConfig(editingId, form)
        : await api.testConnection(form);
      const poolerNote = result.details.pooler_detected ? " (pooler algılandı)" : "";
      setTestResult(result.ok ? `OK — ${String(result.details.version ?? result.message)}${poolerNote}` : result.message);
    } catch (e) {
      setTestResult(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const validate = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    const isWindowsAuth = form.engine === "sqlserver" && form.options?.auth_type === "windows";
    if (!form.name.trim()) errors.name = "Ad zorunlu";
    if (!form.host.trim()) errors.host = "Host zorunlu";
    if (!form.database.trim()) errors.database = "Veritabanı adı zorunlu";
    if (!isWindowsAuth) {
      if (!form.username.trim()) errors.username = "Kullanıcı adı zorunlu";
    }
    return errors;
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const errors = validate();
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      if (editingId !== null) {
        await api.updateInstance(editingId, form);
      }
      setForm(emptyForm(form.engine, isPrivate ? defaultCustomer : undefined));
      setTestResult(null);
      setFieldErrors({});
      setIsFormOpen(false);
      setEditingId(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const askDelete = async (inst: Instance) => {
    setDeleteTarget(inst);
    setDeleteDeps(null);
    setDeleteError(null);
    try {
      setDeleteDeps(await api.getInstanceDependencies(inst.id));
    } catch (e) {
      setDeleteError(String((e as Error).message));
    }
  };

  const confirmDelete = async (cascade: boolean) => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteInstance(deleteTarget.id, cascade);
      setDeleteTarget(null);
      setDeleteDeps(null);
      if (editingId === deleteTarget.id) cancelForm();
      await load();
    } catch (e) {
      setDeleteError(String((e as Error).message));
    } finally {
      setDeleteBusy(false);
    }
  };

  const DEPENDENCY_LABELS: [keyof InstanceDependencies, string][] = [
    ["metric_samples", "Metrik örneği"],
    ["slow_query_samples", "Yavaş sorgu örneği"],
    ["alert_rules", "Alarm kuralı"],
    ["alert_events", "Alarm olayı"],
    ["predictions", "Tahmin"],
    ["metric_rollups", "Günlük metrik özeti"],
    ["schema_object_samples", "Şema nesnesi örneği"],
  ];

  const deletePanel = deleteTarget && (
    <div className="card delete-confirm">
      <h3 className="chart-title">"{deleteTarget.name}" silinsin mi?</h3>
      {deleteError && <div className="error">{deleteError}</div>}
      {!deleteDeps && !deleteError && <p className="muted-note">Bağlı kayıtlar kontrol ediliyor…</p>}
      {deleteDeps && (
        <>
          {deleteDeps.total_records === 0 && deleteDeps.linked_nodes.length === 0 ? (
            <p>Bu instance'a bağlı hiçbir kayıt yok — güvenle silinebilir.</p>
          ) : (
            <>
              <p>Bu instance'a bağlı kayıtlar var. Silerseniz bunlar da silinir:</p>
              <ul className="dependency-list">
                {DEPENDENCY_LABELS.filter(([key]) => (deleteDeps[key] as number) > 0).map(([key, label]) => (
                  <li key={key}>
                    {label}: <strong>{(deleteDeps[key] as number).toLocaleString()}</strong>
                  </li>
                ))}
              </ul>
              {deleteDeps.linked_nodes.length > 0 && (
                <p className="muted-note">
                  {deleteDeps.linked_nodes.length} cluster düğümü bu instance'a bağlı (
                  {deleteDeps.linked_nodes.map((n) => n.name).join(", ")}). Düğümler{" "}
                  <strong>silinmez</strong>, sadece bu veritabanı bağlantıları kopar.
                </p>
              )}
            </>
          )}
        </>
      )}
      <div className="form-actions">
        <button
          className="btn btn-danger"
          disabled={deleteBusy || !deleteDeps}
          onClick={() => confirmDelete((deleteDeps?.total_records ?? 0) > 0 || (deleteDeps?.linked_nodes.length ?? 0) > 0)}
        >
          {deleteBusy
            ? "Siliniyor…"
            : (deleteDeps?.total_records ?? 0) > 0 || (deleteDeps?.linked_nodes.length ?? 0) > 0
              ? "Bağlı kayıtlarla birlikte sil"
              : "Sil"}
        </button>
        <button className="btn" disabled={deleteBusy} onClick={() => { setDeleteTarget(null); setDeleteDeps(null); }}>
          Vazgeç
        </button>
      </div>
    </div>
  );

  const formPanel = (
    <div className="card">
      <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>
        Instance düzenle
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
          Ad <span className="required-mark">*</span>
          <input
            value={form.name}
            onChange={(e) => update("name", e.target.value)}
            className={fieldErrors.name ? "field-invalid" : ""}
          />
          {fieldErrors.name && <span className="field-error">{fieldErrors.name}</span>}
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
        {/* Topoloji AÇIK bir seçim (Faz 22 İŞ 1). Öncesinde form bunu `cluster_name` dolu mu
            diye örtük olarak çıkarıyordu, dolayısıyla cluster alanları standalone bir
            instance'ta da duruyordu. Seçim `standalone`'a alındığında cluster alanları DOM'dan
            çıkıyor ve taşıdıkları değerler temizleniyor — kaydedilirken ilgisiz veri gitmesin. */}
        <label>
          Topoloji
          <select value={topology} onChange={(e) => setTopology(e.target.value as typeof topology)}>
            <option value="standalone">Standalone (tek düğüm)</option>
            <option value="cluster">Cluster üyesi</option>
          </select>
        </label>
        {showField("cluster_name", fieldCtx) && (
          <label>
            Cluster
            <input value={form.cluster_name} onChange={(e) => update("cluster_name", e.target.value)} />
          </label>
        )}
        {showField("role", fieldCtx) && (
          <label>
            Rol
            <input value={form.role} onChange={(e) => update("role", e.target.value)} placeholder="primary, replica, haproxy..." />
          </label>
        )}
        <label>
          Toplama aralığı (saniye, opsiyonel)
          <input
            type="number"
            min={5}
            max={3600}
            value={form.collect_interval_seconds ?? ""}
            onChange={(e) => updateCollectInterval(e.target.value)}
            placeholder="Boş = uygulama genel varsayılanı"
          />
        </label>
        {showField("services", fieldCtx) && (
        <label>
          Sunucu servisleri
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.25rem" }}>
            {PG_SERVICES.map((svc) => (
              <label key={svc} style={{ display: "flex", alignItems: "center", gap: "0.25rem", fontWeight: 400 }}>
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
        )}
        {/* Her alan KENDİ kuralıyla kontrol ediliyor. Dördü bugün aynı kuralı paylaşıyor ama
            tek bir koşula bağlamak, biri değiştiğinde sessizce yanlış olurdu. */}
        {showField("patroni_port", fieldCtx) && (
          <label>
            Patroni port
            <input
              type="number"
              value={form.options?.patroni_port ?? 8008}
              onChange={(e) => updateOption("patroni_port", Number(e.target.value))}
            />
          </label>
        )}
        {showField("etcd_port", fieldCtx) && (
          <label>
            etcd port
            <input
              type="number"
              value={form.options?.etcd_port ?? 2379}
              onChange={(e) => updateOption("etcd_port", Number(e.target.value))}
            />
          </label>
        )}
        {showField("haproxy_stats_port", fieldCtx) && (
          <label>
            HAProxy stats port
            <input
              type="number"
              value={form.options?.haproxy_stats_port ?? 8404}
              onChange={(e) => updateOption("haproxy_stats_port", Number(e.target.value))}
            />
          </label>
        )}
        {showField("haproxy_stats_path", fieldCtx) && (
          <label>
            HAProxy stats path
            <input
              value={form.options?.haproxy_stats_path ?? "/stats;csv"}
              onChange={(e) => updateOption("haproxy_stats_path", e.target.value)}
            />
          </label>
        )}
        {showField("keepalived_vip", fieldCtx) && (
          <label>
            Keepalived VIP
            <input
              value={form.options?.keepalived_vip ?? ""}
              onChange={(e) => updateOption("keepalived_vip", e.target.value)}
              placeholder="10.0.0.50"
            />
          </label>
        )}
        {/* Host agent servis durumu ve log okuma için; topolojiden bağımsız kullanılabilir. */}
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
        {showField("ssl_mode", fieldCtx) && (
            <label>
              SSL modu
              <select
                value={form.options?.ssl_mode ?? "disable"}
                onChange={(e) => updateOption("ssl_mode", e.target.value)}
              >
                <option value="disable">Devre dışı</option>
                <option value="require">Gerekli (require)</option>
              </select>
            </label>
        )}
        {showField("uses_pooler", fieldCtx) && (
            <label>
              Pooler kullanılıyor (PgBouncer / Supabase pooler)
              <select
                value={form.options?.uses_pooler === true ? "true" : form.options?.uses_pooler === false ? "false" : "auto"}
                onChange={(e) =>
                  updateOption(
                    "uses_pooler",
                    e.target.value === "auto" ? null : e.target.value === "true"
                  )
                }
              >
                <option value="auto">Otomatik algıla</option>
                <option value="true">Evet</option>
                <option value="false">Hayır</option>
              </select>
            </label>
        )}
        {showField("auth_type", fieldCtx) && (
          <label>
            Kimlik doğrulama tipi
            <select
              value={form.options?.auth_type ?? "sql"}
              onChange={(e) => updateOption("auth_type", e.target.value)}
            >
              <option value="sql">SQL Server kimlik doğrulama</option>
              <option value="windows">Windows (Integrated)</option>
            </select>
          </label>
        )}
        {showField("replica_set", fieldCtx) && (
          <>
            <label>
              Replica set adı
              <input
                value={form.options?.replica_set ?? ""}
                onChange={(e) => updateOption("replica_set", e.target.value)}
                placeholder="rs0"
              />
            </label>
            <label>
              authSource
              <input
                value={form.options?.authSource ?? ""}
                onChange={(e) => updateOption("authSource", e.target.value)}
                placeholder="admin"
              />
            </label>
          </>
        )}
        <label>
          Host <span className="required-mark">*</span>
          <input
            value={form.host}
            onChange={(e) => update("host", e.target.value)}
            className={fieldErrors.host ? "field-invalid" : ""}
          />
          {fieldErrors.host && <span className="field-error">{fieldErrors.host}</span>}
        </label>
        <label>
          Port
          <input type="number" value={form.port} onChange={(e) => update("port", Number(e.target.value))} />
        </label>
        <label>
          Database <span className="required-mark">*</span>
          <input
            value={form.database}
            onChange={(e) => update("database", e.target.value)}
            className={fieldErrors.database ? "field-invalid" : ""}
          />
          {fieldErrors.database && <span className="field-error">{fieldErrors.database}</span>}
        </label>
        {!(form.engine === "sqlserver" && form.options?.auth_type === "windows") && (
          <>
            <label>
              Kullanıcı <span className="required-mark">*</span>
              <input
                value={form.username}
                onChange={(e) => update("username", e.target.value)}
                className={fieldErrors.username ? "field-invalid" : ""}
              />
              {fieldErrors.username && <span className="field-error">{fieldErrors.username}</span>}
            </label>
            <label>
              Şifre
              <input
                type="password"
                value={form.password}
                onChange={(e) => update("password", e.target.value)}
                placeholder="Değiştirmek için yazın"
                className={fieldErrors.password ? "field-invalid" : ""}
              />
              {fieldErrors.password && <span className="field-error">{fieldErrors.password}</span>}
            </label>
          </>
        )}
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
            Güncelle
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
        {/* Birincil eylem her sayfada aynı kapta: sağ üstte, .header-actions içinde
            (Faz 22 İŞ 2). Burada doğrudan header'ın çocuğuydu, hizası diğer
            sayfalardan farklıydı. */}
        {canWrite && (
          <div className="header-actions">
            <Link to="/customers" className="btn btn-primary">+ Veritabanı Ekle</Link>
          </div>
        )}
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
                <TableState
                  colSpan={isPrivate ? 7 : 8}
                  loading={!loaded}
                  error={listError}
                  onRetry={load}
                  title="Kayıtlı instance yok"
                  detail={
                    canWrite
                      ? "Bir instance, bağlantı bilgileriyle izlenen tek bir veritabanıdır. Yukarıdaki formdan ilkini ekleyin; metrikler ilk toplama döngüsünden sonra görünür."
                      : "Bir instance, bağlantı bilgileriyle izlenen tek bir veritabanıdır. Instance eklemek admin yetkisi gerektirir."
                  }
                />
              ) : (
                instanceSlice.items.map((inst) => (
                  <tr key={inst.id}>
                    <td><Link to={`/instances/${inst.id}`}>{inst.name}</Link></td>
                    {!isPrivate && <td>{inst.customer_name || "—"}</td>}
                    <td>{inst.environment}</td>
                    <td>{inst.application || "—"}</td>
                    <td>{inst.cluster_name || "—"}</td>
                    <td>{inst.role || "—"}</td>
                    <td>{inst.engine}</td>
                    <td>
                      {canWrite && (
                        <div style={{ display: "flex", gap: "0.5rem" }}>
                          <button className="btn" onClick={() => startEdit(inst)}>Düzenle</button>
                          <button className="btn btn-danger" onClick={() => askDelete(inst)}>Sil</button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <Pagination slice={instanceSlice} label="instance" />

        {deleteTarget ? (
          deletePanel
        ) : isFormOpen ? (
          formPanel
        ) : (
          <div className="card" style={{ display: "grid", placeItems: "center", minHeight: 200 }}>
            <p style={{ color: "var(--muted)", textAlign: "center" }}>
              Yeni instance eklemek için <Link to="/customers">Müşteriler</Link> → Uygulamalar →
              bir veritabanı grubuna sihirbazla düğüm ekleyin. Burada listedeki{" "}
              <strong>Düzenle</strong> butonuyla var olan instance'ları düzenleyebilirsiniz.
            </p>
          </div>
        )}
      </div>
    </>
  );
}
