import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Application, DatabaseGroup, DbEngine, GroupEnvironment, GroupTopology } from "../api";

const ENV_LABELS: Record<GroupEnvironment, string> = {
  prod: "Prod",
  preprod: "Preprod",
  test: "Test",
  dev: "Dev",
};

export default function DatabaseGroupsPage() {
  const { applicationId } = useParams<{ applicationId: string }>();
  const id = Number(applicationId);

  const [application, setApplication] = useState<Application | null>(null);
  const [groups, setGroups] = useState<DatabaseGroup[]>([]);
  const [name, setName] = useState("");
  const [engine, setEngine] = useState<DbEngine>("postgresql");
  const [topology, setTopology] = useState<GroupTopology>("patroni");
  const [environment, setEnvironment] = useState<GroupEnvironment>("prod");
  const [accessName, setAccessName] = useState("");
  const [clusterName, setClusterName] = useState("");
  const [vipAddress, setVipAddress] = useState("");
  const [listenerPort, setListenerPort] = useState<number | "">("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editEnvironment, setEditEnvironment] = useState<GroupEnvironment>("prod");
  const [editAccessName, setEditAccessName] = useState("");
  const [editNotes, setEditNotes] = useState("");

  const load = () => api.getGroups(id).then(setGroups).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    api.getApplication(id).then(setApplication).catch(() => undefined);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const validateCreate = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    if (!name.trim()) errors.name = "Grup adı zorunlu";
    if (topology !== "standalone") {
      if (!accessName.trim()) errors.access_name = "Erişim adı (listener/VIP) zorunlu";
      if (!clusterName.trim()) errors.cluster_name = "Cluster adı zorunlu";
    }
    return errors;
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const errors = validateCreate();
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      await api.createGroup({
        application_id: id,
        name,
        engine,
        topology,
        environment,
        access_name: accessName || undefined,
        cluster_name: clusterName || undefined,
        vip_address: vipAddress || undefined,
        listener_port: listenerPort === "" ? undefined : Number(listenerPort),
        notes: notes || undefined,
      });
      setName("");
      setAccessName("");
      setClusterName("");
      setVipAddress("");
      setListenerPort("");
      setNotes("");
      setFieldErrors({});
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (groupId: number) => {
    if (!confirm("Grup ve altındaki tüm düğüm kayıtları silinsin mi?")) return;
    await api.deleteGroup(groupId);
    await load();
  };

  const startEdit = (g: DatabaseGroup) => {
    setEditingId(g.id);
    setEditName(g.name);
    setEditEnvironment(g.environment);
    setEditAccessName(g.access_name || "");
    setEditNotes(g.notes || "");
    setError(null);
  };

  const saveEdit = async (groupId: number) => {
    if (!editName.trim()) {
      setError("Grup adı zorunlu");
      return;
    }
    try {
      await api.updateGroup(groupId, {
        name: editName,
        environment: editEnvironment,
        access_name: editAccessName || undefined,
        notes: editNotes || undefined,
      });
      setEditingId(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Database Groups {application && <span className="detail-meta">— {application.name}</span>}</h2>
          <p>
            {application && <Link to={`/customers/${application.customer_id}/applications`}>← Uygulamalar</Link>}
          </p>
        </div>
        <div className="header-actions">
          <Link to={`/applications/${id}/groups/wizard`} className="btn btn-primary">+ Veritabanı Ekle (Sihirbaz)</Link>
          <a href="#new-group-form" className="btn">Manuel grup ekle</a>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ad / Erişim</th>
                <th>Motor</th>
                <th>Topoloji</th>
                <th>Durum</th>
                <th>Ortam</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {groups.length === 0 ? (
                <tr>
                  <td colSpan={6} className="empty">Kayıtlı grup yok</td>
                </tr>
              ) : (
                groups.map((g) => {
                  const isCluster = g.topology !== "standalone";
                  const status = g.status;
                  if (editingId === g.id) {
                    return (
                      <tr key={g.id}>
                        <td>
                          <input value={editName} onChange={(e) => setEditName(e.target.value)} style={{ marginBottom: "0.3rem" }} />
                          {isCluster && (
                            <input
                              value={editAccessName}
                              onChange={(e) => setEditAccessName(e.target.value)}
                              placeholder="Erişim adı"
                            />
                          )}
                        </td>
                        <td><span className="engine-badge">{g.engine}</span></td>
                        <td>{g.topology}</td>
                        <td>
                          <input value={editNotes} onChange={(e) => setEditNotes(e.target.value)} placeholder="Notlar" />
                        </td>
                        <td>
                          <select value={editEnvironment} onChange={(e) => setEditEnvironment(e.target.value as GroupEnvironment)}>
                            <option value="prod">Prod</option>
                            <option value="preprod">Preprod</option>
                            <option value="test">Test</option>
                            <option value="dev">Dev</option>
                          </select>
                        </td>
                        <td>
                          <div style={{ display: "flex", gap: "0.3rem" }}>
                            <button className="btn btn-primary" onClick={() => saveEdit(g.id)}>Kaydet</button>
                            <button className="btn" onClick={() => setEditingId(null)}>Vazgeç</button>
                          </div>
                        </td>
                      </tr>
                    );
                  }
                  return (
                    <tr key={g.id}>
                      <td>
                        <Link to={`/groups/${g.id}`}>{isCluster && g.access_name ? g.access_name : g.name}</Link>
                        {isCluster && g.access_name && <div className="instance-meta">{g.name}</div>}
                      </td>
                      <td>
                        <span className="engine-badge">{g.engine}</span>
                      </td>
                      <td>{g.topology}</td>
                      <td>
                        {status ? (
                          <div style={{ display: "flex", flexDirection: "column", gap: "0.15rem" }}>
                            <span className={`tuning-status ${status.overall}`}>{status.overall}</span>
                            {isCluster && (
                              <span className="muted-note">
                                {status.nodes_up}/{status.nodes_up + status.nodes_down} up
                                {status.primary_node ? ` · primary: ${status.primary_node}` : ""}
                                {status.replication_lag_bytes != null ? ` · lag: ${status.replication_lag_bytes}` : ""}
                              </span>
                            )}
                          </div>
                        ) : (
                          <span className="muted-note">—</span>
                        )}
                      </td>
                      <td>
                        <span className={`env-badge ${g.environment}`}>{ENV_LABELS[g.environment]}</span>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          <button className="btn" onClick={() => startEdit(g)}>Düzenle</button>
                          <button className="btn btn-danger" onClick={() => onDelete(g.id)}>Sil</button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="card" id="new-group-form">
          <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni database group</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Ad <span className="required-mark">*</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="boa-sqlserver-ag"
                className={fieldErrors.name ? "field-invalid" : ""}
              />
              {fieldErrors.name && <span className="field-error">{fieldErrors.name}</span>}
            </label>
            <label>
              Motor
              <select value={engine} onChange={(e) => setEngine(e.target.value as DbEngine)}>
                <option value="postgresql">PostgreSQL</option>
                <option value="sqlserver">SQL Server</option>
                <option value="mongodb">MongoDB</option>
              </select>
            </label>
            <label>
              Topoloji
              <select value={topology} onChange={(e) => setTopology(e.target.value as GroupTopology)}>
                <option value="standalone">Standalone</option>
                <option value="patroni">Patroni (PostgreSQL cluster)</option>
                <option value="alwayson">Always On (SQL Server AG)</option>
              </select>
            </label>
            <label>
              Ortam
              <select value={environment} onChange={(e) => setEnvironment(e.target.value as GroupEnvironment)}>
                <option value="prod">Prod</option>
                <option value="preprod">Preprod</option>
                <option value="test">Test</option>
                <option value="dev">Dev</option>
              </select>
            </label>
            {topology !== "standalone" && (
              <>
                <label>
                  Erişim adı (listener / VIP) <span className="required-mark">*</span>
                  <input
                    value={accessName}
                    onChange={(e) => setAccessName(e.target.value)}
                    placeholder="boa-ag-listener.internal"
                    className={fieldErrors.access_name ? "field-invalid" : ""}
                  />
                  {fieldErrors.access_name && <span className="field-error">{fieldErrors.access_name}</span>}
                </label>
                <label>
                  Cluster adı <span className="required-mark">*</span>
                  <input
                    value={clusterName}
                    onChange={(e) => setClusterName(e.target.value)}
                    placeholder="boa-ag"
                    className={fieldErrors.cluster_name ? "field-invalid" : ""}
                  />
                  {fieldErrors.cluster_name && <span className="field-error">{fieldErrors.cluster_name}</span>}
                </label>
                <label>
                  VIP / IP
                  <input
                    value={vipAddress}
                    onChange={(e) => setVipAddress(e.target.value)}
                    placeholder="10.0.0.50"
                  />
                </label>
                <label>
                  {engine === "sqlserver" ? "Listener portu" : "VIP / HAProxy portu"}
                  <input
                    type="number"
                    value={listenerPort}
                    onChange={(e) => setListenerPort(e.target.value === "" ? "" : Number(e.target.value))}
                    placeholder={engine === "sqlserver" ? "1433" : "5000"}
                  />
                </label>
              </>
            )}
            <label>
              Notlar
              <input value={notes} onChange={(e) => setNotes(e.target.value)} />
            </label>
            <div className="form-actions">
              <button type="submit" className="btn btn-primary" disabled={busy}>
                Ekle
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}
