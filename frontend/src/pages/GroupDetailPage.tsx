import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlwaysOnHealth,
  api,
  Application,
  DatabaseGroup,
  DbNode,
  GroupHealth,
  NodeCreate,
  NodeRoleHint,
  NodeSite,
  ParameterAudit,
} from "../api";

type Tab = "nodes" | "parameters" | "alwayson";

const emptyNodeForm = (groupId: number): NodeCreate => ({
  group_id: groupId,
  name: "",
  host: "",
  port: 5432,
  site: "primary",
  role_hint: "unknown",
  agent_url: "",
  agent_token: "",
});

const STATUS_TR: Record<string, string> = { up: "UP", down: "DOWN", unknown: "UNKNOWN", skipped: "SKIP" };

export default function GroupDetailPage() {
  const { groupId } = useParams<{ groupId: string }>();
  const id = Number(groupId);

  const [group, setGroup] = useState<DatabaseGroup | null>(null);
  const [application, setApplication] = useState<Application | null>(null);
  const [nodes, setNodes] = useState<DbNode[]>([]);
  const [nodeForm, setNodeForm] = useState<NodeCreate>(emptyNodeForm(id));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<Tab>("nodes");

  const [health, setHealth] = useState<GroupHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);

  const [params, setParams] = useState<ParameterAudit | null>(null);
  const [paramsError, setParamsError] = useState<string | null>(null);
  const [paramsLoading, setParamsLoading] = useState(false);

  const [alwaysOn, setAlwaysOn] = useState<AlwaysOnHealth | null>(null);
  const [alwaysOnError, setAlwaysOnError] = useState<string | null>(null);
  const [alwaysOnLoading, setAlwaysOnLoading] = useState(false);

  const loadNodes = () => api.getGroupNodes(id).then(setNodes).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    api.getGroup(id).then((g) => {
      setGroup(g);
      setNodeForm((prev) => ({ ...prev, port: g.engine === "sqlserver" ? 1433 : g.engine === "mongodb" ? 27017 : 5432 }));
      api.getApplication(g.application_id).then(setApplication).catch(() => undefined);
    }).catch((e) => setError(String(e.message || e)));
    loadNodes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const onAddNode = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createNode({
        ...nodeForm,
        group_id: id,
        agent_url: nodeForm.agent_url || undefined,
        agent_token: nodeForm.agent_token || undefined,
      });
      setNodeForm(emptyNodeForm(id));
      await loadNodes();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDeleteNode = async (nodeId: number) => {
    if (!confirm("Düğüm silinsin mi?")) return;
    await api.deleteNode(nodeId);
    await loadNodes();
  };

  const loadHealth = async () => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      setHealth(await api.getGroupHealth(id));
    } catch (e) {
      setHealth(null);
      setHealthError(String((e as Error).message || e));
    } finally {
      setHealthLoading(false);
    }
  };

  const loadParams = async () => {
    setParamsLoading(true);
    setParamsError(null);
    try {
      setParams(await api.getGroupParameters(id));
    } catch (e) {
      setParams(null);
      setParamsError(String((e as Error).message || e));
    } finally {
      setParamsLoading(false);
    }
  };

  const loadAlwaysOn = async () => {
    setAlwaysOnLoading(true);
    setAlwaysOnError(null);
    try {
      setAlwaysOn(await api.getGroupAlwaysOn(id));
    } catch (e) {
      setAlwaysOn(null);
      setAlwaysOnError(String((e as Error).message || e));
    } finally {
      setAlwaysOnLoading(false);
    }
  };

  const healthByNodeId = new Map((health?.nodes ?? []).map((n) => [n.node_id, n]));

  return (
    <>
      <header className="page-header detail-header">
        <div>
          <div className="detail-title-row">
            <h2>{group?.name ?? "Database Group"}</h2>
            {group && <span className="engine-badge">{group.engine}</span>}
            {group && <span className="tag">{group.topology}</span>}
            {group && <span className={`env-badge ${group.environment}`}>{group.environment}</span>}
          </div>
          <p className="detail-subtitle">
            {application && <Link to={`/applications/${application.id}/groups`}>← {application.name}</Link>}
            {group?.access_name && <span className="detail-meta"> · Erişim: {group.access_name}</span>}
          </p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="detail-tabs">
        <button className={`tab-btn${tab === "nodes" ? " active" : ""}`} onClick={() => setTab("nodes")}>
          Düğümler
        </button>
        {group?.engine === "postgresql" && (
          <button className={`tab-btn${tab === "parameters" ? " active" : ""}`} onClick={() => setTab("parameters")}>
            Parametreler
          </button>
        )}
        {group?.engine === "sqlserver" && (
          <button className={`tab-btn${tab === "alwayson" ? " active" : ""}`} onClick={() => setTab("alwayson")}>
            Always On
          </button>
        )}
      </div>

      {tab === "nodes" && (
        <div className="cluster-layout">
          <div className="activity-toolbar">
            <h3 className="chart-title" style={{ margin: 0 }}>
              Düğüm sağlığı
              {health && (
                <span className={`tuning-status ${health.overall}`} style={{ marginLeft: "0.6rem" }}>
                  {health.overall}
                </span>
              )}
            </h3>
            <button className="btn" onClick={loadHealth} disabled={healthLoading || nodes.length === 0}>
              {healthLoading ? "Kontrol ediliyor…" : "Sağlığı kontrol et"}
            </button>
          </div>

          {healthError && <div className="error">{healthError}</div>}

          {health && (
            <div className="stats-grid compact">
              <div className="card stat-tile">
                <div className="stat-tile-label">UP</div>
                <div className="stat-tile-value">{health.totals.up}</div>
              </div>
              <div className="card stat-tile">
                <div className="stat-tile-label">DOWN</div>
                <div className={`stat-tile-value${health.totals.down ? " danger-text" : ""}`}>{health.totals.down}</div>
              </div>
              <div className="card stat-tile">
                <div className="stat-tile-label">etcd quorum</div>
                <div className={`stat-tile-value${health.etcd_quorum.has_quorum ? "" : " danger-text"}`}>
                  {health.etcd_quorum.up}/{health.etcd_quorum.total}
                </div>
              </div>
              <div className="card stat-tile">
                <div className="stat-tile-label">Split-brain</div>
                <div className={`stat-tile-value${health.split_brain ? " danger-text" : " ok-text"}`}>
                  {health.split_brain ? "EVET" : "Hayır"}
                </div>
              </div>
            </div>
          )}

          {health?.split_brain && (
            <div className="error">
              Split-brain şüphesi: VIP'i aynı anda tutan düğümler — {health.split_brain_nodes.join(", ")}
            </div>
          )}

          {health?.cluster && (
            <div className="card">
              <h3 className="chart-title">Patroni cluster</h3>
              <p className="muted-note">
                Leader: <strong>{health.cluster.leader || "yok"}</strong> · Members: {health.cluster.member_count}
              </p>
              {health.cluster.members.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>Node</th><th>Rol</th><th>Durum</th><th>Host</th><th>Lag</th></tr>
                    </thead>
                    <tbody>
                      {health.cluster.members.map((m) => (
                        <tr key={`${m.name}-${m.host}`}>
                          <td>{m.name || "—"}</td>
                          <td>{m.role || "—"}</td>
                          <td>{m.state || "—"}</td>
                          <td>{m.host || "—"}</td>
                          <td>{m.lag != null ? m.lag : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          <div className="card">
            <h3 className="chart-title">Düğümler</h3>
            <div className="cluster-service-grid">
              {nodes.length === 0 && <p className="muted-note">Henüz düğüm eklenmedi.</p>}
              {nodes.map((node) => {
                const nodeHealth = healthByNodeId.get(node.id);
                return (
                  <div key={node.id} className={`cluster-service-card${node.site === "disaster" ? " status-unknown" : ""}`}>
                    <div className="cluster-service-head">
                      <strong>{node.name}</strong>
                      <div style={{ display: "flex", gap: "0.3rem" }}>
                        <span className={`tag ${node.site === "disaster" ? "private" : "public"}`}>
                          {node.site === "disaster" ? "DR" : "primary site"}
                        </span>
                        <span className="tag service">{node.role_hint}</span>
                      </div>
                    </div>
                    <p className="muted-note">{node.host}:{node.port}</p>
                    {node.agent_url && <p className="muted-note">agent: {node.agent_url}</p>}
                    {nodeHealth ? (
                      <div className="cluster-service-meta" style={{ flexDirection: "column", alignItems: "flex-start", gap: "0.3rem" }}>
                        {nodeHealth.services.map((svc) => (
                          <span key={svc.service} className={`state-pill ${svc.status}`}>
                            {svc.service}: {STATUS_TR[svc.status] || svc.status}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <p className="muted-note">Sağlık verisi için üstteki butonu kullanın.</p>
                    )}
                    <button className="btn btn-danger" style={{ marginTop: "0.5rem" }} onClick={() => onDeleteNode(node.id)}>
                      Sil
                    </button>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="card">
            <h3 className="chart-title">Yeni düğüm</h3>
            <form className="form-grid" onSubmit={onAddNode}>
              <label>
                Ad
                <input value={nodeForm.name} onChange={(e) => setNodeForm({ ...nodeForm, name: e.target.value })} required />
              </label>
              <label>
                Host
                <input value={nodeForm.host} onChange={(e) => setNodeForm({ ...nodeForm, host: e.target.value })} required />
              </label>
              <label>
                Port
                <input
                  type="number"
                  value={nodeForm.port}
                  onChange={(e) => setNodeForm({ ...nodeForm, port: Number(e.target.value) })}
                />
              </label>
              <label>
                Site
                <select value={nodeForm.site} onChange={(e) => setNodeForm({ ...nodeForm, site: e.target.value as NodeSite })}>
                  <option value="primary">Primary (ana DC)</option>
                  <option value="disaster">Disaster (DR)</option>
                </select>
              </label>
              <label>
                Rol
                <select
                  value={nodeForm.role_hint}
                  onChange={(e) => setNodeForm({ ...nodeForm, role_hint: e.target.value as NodeRoleHint })}
                >
                  <option value="unknown">Bilinmiyor</option>
                  <option value="primary">Primary</option>
                  <option value="replica">Replica</option>
                </select>
              </label>
              <label>
                Host agent URL (opsiyonel)
                <input
                  value={nodeForm.agent_url}
                  onChange={(e) => setNodeForm({ ...nodeForm, agent_url: e.target.value })}
                  placeholder="http://db-host:9105"
                />
              </label>
              <label>
                Host agent token (opsiyonel)
                <input
                  type="password"
                  value={nodeForm.agent_token}
                  onChange={(e) => setNodeForm({ ...nodeForm, agent_token: e.target.value })}
                />
              </label>
              <div className="form-actions">
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  Ekle
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {tab === "parameters" && (
        <div className="cluster-layout">
          <div className="activity-toolbar">
            <h3 className="chart-title" style={{ margin: 0 }}>PostgreSQL parametre denetimi</h3>
            <button className="btn" onClick={loadParams} disabled={paramsLoading}>
              {paramsLoading ? "Denetleniyor…" : "Parametreleri denetle"}
            </button>
          </div>
          {paramsError && <div className="error">{paramsError}</div>}
          {params && (
            <>
              <p className="muted-note">
                Düğüm: {params.node_name} · {new Date(params.checked_at).toLocaleString()}
              </p>
              <div className="stats-grid compact">
                {Object.entries(params.summary).map(([sev, count]) => (
                  <div key={sev} className="card stat-tile">
                    <div className="stat-tile-label">{sev}</div>
                    <div className="stat-tile-value">{count}</div>
                  </div>
                ))}
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Parametre</th><th>Kategori</th><th>Değer</th><th>Önem</th><th>Öneri</th><th>Detay</th></tr>
                  </thead>
                  <tbody>
                    {params.findings.map((f) => (
                      <tr key={f.name}>
                        <td><code>{f.name}</code></td>
                        <td>{f.category}</td>
                        <td>{f.current_value ?? "—"}</td>
                        <td><span className={`status ${f.severity}`}>{f.severity}</span></td>
                        <td>{f.recommendation}</td>
                        <td className="muted-note">{f.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {params.patroni_config && (
                <div className="card">
                  <h3 className="chart-title">Patroni /config</h3>
                  <pre className="cluster-log-view">{JSON.stringify(params.patroni_config, null, 2)}</pre>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {tab === "alwayson" && (
        <div className="cluster-layout">
          <div className="activity-toolbar">
            <h3 className="chart-title" style={{ margin: 0 }}>Always On Availability Group</h3>
            <button className="btn" onClick={loadAlwaysOn} disabled={alwaysOnLoading}>
              {alwaysOnLoading ? "Getiriliyor…" : "Always On durumunu getir"}
            </button>
          </div>
          {alwaysOnError && <div className="error">{alwaysOnError}</div>}
          {alwaysOn && (
            <>
              <p className="muted-note">
                AG: <strong>{alwaysOn.ag_name || "—"}</strong> · Primary: {alwaysOn.primary_replica || "—"} · Sync health:{" "}
                {alwaysOn.ag_sync_health || "—"} ·{" "}
                <span className={`tuning-status ${alwaysOn.overall}`}>{alwaysOn.overall}</span>
              </p>
              <div className="cluster-service-grid">
                {alwaysOn.replicas.map((r) => (
                  <div
                    key={r.replica_server_name}
                    className={`cluster-service-card${r.site === "disaster" ? " status-unknown" : r.failover_ready ? " status-up" : " status-down"}`}
                  >
                    <div className="cluster-service-head">
                      <strong>{r.node_name || r.replica_server_name}</strong>
                      <div style={{ display: "flex", gap: "0.3rem" }}>
                        {r.site && (
                          <span className={`tag ${r.site === "disaster" ? "private" : "public"}`}>
                            {r.site === "disaster" ? "DR" : "primary site"}
                          </span>
                        )}
                        <span className="tag service">{r.role || "—"}</span>
                      </div>
                    </div>
                    <p className="muted-note">
                      {r.availability_mode || "—"} · {r.connected_state || "—"} · sync: {r.sync_health || "—"}
                    </p>
                    <p className={r.failover_ready ? "ok-text" : "warn-text"}>
                      {r.failover_ready ? "Failover'a hazır (RPO=0)" : "Failover riskli / hazır değil"}
                    </p>
                    {r.databases.length > 0 && (
                      <table style={{ marginTop: "0.5rem" }}>
                        <thead>
                          <tr><th>DB</th><th>Durum</th><th>Log queue</th><th>Redo queue</th></tr>
                        </thead>
                        <tbody>
                          {r.databases.map((d) => (
                            <tr key={d.database_name}>
                              <td>{d.database_name}</td>
                              <td>{d.synchronization_state || "—"}</td>
                              <td>{d.log_send_queue_kb != null ? `${d.log_send_queue_kb} KB` : "—"}</td>
                              <td>{d.redo_queue_kb != null ? `${d.redo_queue_kb} KB` : "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}
