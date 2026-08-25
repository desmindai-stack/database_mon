import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Customer, DbServer, NodeSite, ServerOS } from "../api";

const OS_LABELS: Record<ServerOS, string> = { linux: "Linux", windows: "Windows" };
const SITE_LABELS: Record<NodeSite, string> = { primary: "Ana DC", disaster: "Disaster (DR)" };

export default function ServersPage() {
  const { customerId } = useParams<{ customerId: string }>();
  const id = Number(customerId);

  const [customer, setCustomer] = useState<Customer | null>(null);
  const [servers, setServers] = useState<DbServer[]>([]);
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [os, setOs] = useState<ServerOS>("linux");
  const [site, setSite] = useState<NodeSite>("primary");
  const [agentUrl, setAgentUrl] = useState("");
  const [agentToken, setAgentToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editHost, setEditHost] = useState("");
  const [editOs, setEditOs] = useState<ServerOS>("linux");
  const [editSite, setEditSite] = useState<NodeSite>("primary");

  const load = () => api.getServers(id).then(setServers).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    api.getCustomers().then((all) => setCustomer(all.find((c) => c.id === id) ?? null));
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createServer({
        customer_id: id,
        name,
        host,
        os,
        site,
        agent_url: agentUrl || undefined,
        agent_token: agentToken || undefined,
      });
      setName("");
      setHost("");
      setAgentUrl("");
      setAgentToken("");
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (serverId: number) => {
    if (!confirm("Sunucu silinsin mi?")) return;
    try {
      await api.deleteServer(serverId);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const startEdit = (s: DbServer) => {
    setEditingId(s.id);
    setEditName(s.name);
    setEditHost(s.host);
    setEditOs(s.os);
    setEditSite(s.site);
  };

  const saveEdit = async (serverId: number) => {
    try {
      await api.updateServer(serverId, { name: editName, host: editHost, os: editOs, site: editSite });
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
          <h2>Sunucular {customer && <span className="detail-meta">— {customer.name}</span>}</h2>
          <p>
            <Link to={`/customers/${id}/applications`}>← Uygulamalar</Link>
          </p>
        </div>
        <div className="header-actions">
          <a href="#new-server-form" className="btn btn-primary">+ Sunucu Ekle</a>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ad</th>
                <th>Host</th>
                <th>OS</th>
                <th>Site</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {servers.length === 0 ? (
                <tr>
                  <td colSpan={5} className="empty">Kayıtlı sunucu yok</td>
                </tr>
              ) : (
                servers.map((s) =>
                  editingId === s.id ? (
                    <tr key={s.id}>
                      <td><input value={editName} onChange={(e) => setEditName(e.target.value)} /></td>
                      <td><input value={editHost} onChange={(e) => setEditHost(e.target.value)} /></td>
                      <td>
                        <select value={editOs} onChange={(e) => setEditOs(e.target.value as ServerOS)}>
                          <option value="linux">Linux</option>
                          <option value="windows">Windows</option>
                        </select>
                      </td>
                      <td>
                        <select value={editSite} onChange={(e) => setEditSite(e.target.value as NodeSite)}>
                          <option value="primary">Ana DC</option>
                          <option value="disaster">Disaster (DR)</option>
                        </select>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          <button className="btn btn-primary" onClick={() => saveEdit(s.id)}>Kaydet</button>
                          <button className="btn" onClick={() => setEditingId(null)}>Vazgeç</button>
                        </div>
                      </td>
                    </tr>
                  ) : (
                    <tr key={s.id}>
                      <td>{s.name}</td>
                      <td>{s.host}</td>
                      <td><span className="engine-badge">{OS_LABELS[s.os]}</span></td>
                      <td>
                        <span className={`tag ${s.site === "disaster" ? "private" : "public"}`}>
                          {SITE_LABELS[s.site]}
                        </span>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          <button className="btn" onClick={() => startEdit(s)}>Düzenle</button>
                          <button className="btn btn-danger" onClick={() => onDelete(s.id)}>Sil</button>
                        </div>
                      </td>
                    </tr>
                  )
                )
              )}
            </tbody>
          </table>
        </div>

        <div className="card" id="new-server-form">
          <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni sunucu</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Ad
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="boa-winsvr-01" required />
            </label>
            <label>
              Host
              <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="boa-winsvr-01.internal" required />
            </label>
            <label>
              İşletim sistemi
              <select value={os} onChange={(e) => setOs(e.target.value as ServerOS)}>
                <option value="linux">Linux</option>
                <option value="windows">Windows</option>
              </select>
            </label>
            <label>
              Site
              <select value={site} onChange={(e) => setSite(e.target.value as NodeSite)}>
                <option value="primary">Ana DC</option>
                <option value="disaster">Disaster (DR)</option>
              </select>
            </label>
            <label>
              Host agent URL (opsiyonel)
              <input value={agentUrl} onChange={(e) => setAgentUrl(e.target.value)} placeholder="http://boa-winsvr-01:9105" />
            </label>
            <label>
              Host agent token (opsiyonel)
              <input type="password" value={agentToken} onChange={(e) => setAgentToken(e.target.value)} />
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
