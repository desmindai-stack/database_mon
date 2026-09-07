import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Customer, DbServer, NodeSite, ServerOS } from "../api";
import { NotFoundState, PageError, PageLoading, TableState } from "../components/PageState";
import { useAuth } from "../auth";

const OS_LABELS: Record<ServerOS, string> = { linux: "Linux", windows: "Windows" };
const SITE_LABELS: Record<NodeSite, string> = { primary: "Ana DC", disaster: "Disaster (DR)" };

export default function ServersPage() {
  const { customerId } = useParams<{ customerId: string }>();
  const id = Number(customerId);
  const idIsValid = Number.isInteger(id) && id > 0;
  const canWrite = useAuth().user?.role === "admin";

  const [customer, setCustomer] = useState<Customer | null>(null);
  const [servers, setServers] = useState<DbServer[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [rowAgentResult, setRowAgentResult] = useState<{ id: number; ok: boolean; message: string } | null>(null);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editHost, setEditHost] = useState("");
  const [editIpAddress, setEditIpAddress] = useState("");
  const [editOs, setEditOs] = useState<ServerOS>("linux");
  const [editSite, setEditSite] = useState<NodeSite>("primary");
  // Faz 16-B İŞ 2: agent bilgileri de düzenlenebilsin — daha önce sadece sihirbazda
  // girilebiliyordu, sonradan değiştirmek için sunucuyu silmek gerekiyordu.
  const [editAgentUrl, setEditAgentUrl] = useState("");
  const [editAgentToken, setEditAgentToken] = useState("");

  const load = () =>
    api
      .getServers(id)
      .then((rows) => {
        setServers(rows);
        setListError(null);
      })
      .catch(setListError)
      .finally(() => setLoaded(true));

  useEffect(() => {
    if (!idIsValid) return;
    // Yakalanmamis promise reddi + "listede yok" durumunun ele alinmamasi (Faz 19 IS 1) —
    // bkz. ApplicationsPage'deki ayni duzeltme.
    api.getCustomers().then((all) => {
      const found = all.find((c) => c.id === id) ?? null;
      setCustomer(found);
      setNotFound(found === null);
    }).catch((e) => setError(String((e as Error).message || e)));
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, idIsValid, reloadKey]);

  const onTestRowAgent = async (s: DbServer) => {
    try {
      const result = await api.testExistingServerAgent(s.id);
      setRowAgentResult({ id: s.id, ok: result.ok, message: result.message });
    } catch (err) {
      setRowAgentResult({ id: s.id, ok: false, message: String((err as Error).message) });
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
    setEditIpAddress(s.ip_address ?? "");
    setEditOs(s.os);
    setEditSite(s.site);
    setEditAgentUrl(s.agent_url ?? "");
    setEditAgentToken(s.agent_token ?? "");
    setError(null);
  };

  const saveEdit = async (serverId: number) => {
    if (!editName.trim() || !editHost.trim()) {
      setError("Sunucu adı ve hostname zorunlu");
      return;
    }
    try {
      await api.updateServer(serverId, {
        name: editName,
        host: editHost,
        ip_address: editIpAddress || null,
        os: editOs,
        site: editSite,
        agent_url: editAgentUrl || null,
        agent_token: editAgentToken || null,
      });
      setEditingId(null);
      setError(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  if (!idIsValid || notFound) {
    return (
      <NotFoundState
        title="Müşteri bulunamadı"
        detail={
          idIsValid
            ? `#${id} numaralı müşteri yok — silinmiş olabilir ya da bağlantı eskimiş olabilir.`
            : `"${customerId}" geçerli bir müşteri numarası değil.`
        }
        backTo="/customers"
        backLabel="Müşteri listesine dön"
      />
    );
  }

  if (!customer) {
    return error ? <PageError error={error} onRetry={() => setReloadKey((k) => k + 1)} /> : <PageLoading />;
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Sunucular {customer && <span className="detail-meta">— {customer.name}</span>}</h2>
          <p>
            <Link to={`/customers/${id}/applications`}>← Uygulamalar</Link>
          </p>
        </div>
      </header>

      <p className="muted-note" style={{ marginBottom: "1rem" }}>
        Sunucu tek başına eklenmiyor — bir veritabanı instance'ı eklerken "Yeni sunucu"yu
        seçtiğinizde otomatik oluşturuluyor (bkz. "+ Veritabanı Ekle" sihirbazı). Burada sadece
        var olan sunucuları düzenleyebilir/silebilirsiniz.
      </p>

      {error && <div className="error">{error}</div>}
      {rowAgentResult && (
        <div className={rowAgentResult.ok ? "ok-text" : "warn-text"} style={{ marginBottom: "0.75rem" }}>
          {servers.find((s) => s.id === rowAgentResult.id)?.name}: {rowAgentResult.message}
        </div>
      )}

      <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ad</th>
                <th>Host</th>
                <th>IP</th>
                <th>OS</th>
                <th>Site</th>
                <th>Agent</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {servers.length === 0 ? (
                <TableState
                  colSpan={7}
                  loading={!loaded}
                  error={listError}
                  onRetry={load}
                  title="Kayıtlı sunucu yok"
                  detail="Sunucular veritabanı sihirbazında düğüm eklerken oluşturulur; buradan da elle eklenebilir."
                />
              ) : (
                servers.map((s) =>
                  editingId === s.id ? (
                    <tr key={s.id}>
                      <td><input value={editName} onChange={(e) => setEditName(e.target.value)} required /></td>
                      <td><input value={editHost} onChange={(e) => setEditHost(e.target.value)} required /></td>
                      <td><input value={editIpAddress} onChange={(e) => setEditIpAddress(e.target.value)} placeholder="10.0.0.1" /></td>
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
                        <div style={{ display: "grid", gap: "0.25rem" }}>
                          <input
                            value={editAgentUrl}
                            onChange={(e) => setEditAgentUrl(e.target.value)}
                            placeholder="http://host:9105"
                          />
                          <input
                            type="password"
                            value={editAgentToken}
                            onChange={(e) => setEditAgentToken(e.target.value)}
                            placeholder="agent token"
                          />
                        </div>
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
                      <td className="muted-note">{s.ip_address || "—"}</td>
                      <td><span className="engine-badge">{OS_LABELS[s.os]}</span></td>
                      <td>
                        <span className={`tag ${s.site === "disaster" ? "private" : "public"}`}>
                          {SITE_LABELS[s.site]}
                        </span>
                      </td>
                      <td className="muted-note">{s.agent_url || "—"}</td>
                      <td>
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          <button
                            className="btn btn-xs"
                            disabled={!s.agent_url}
                            title={s.agent_url ? "Agent'a bağlan ve servis listesini kontrol et" : "agent_url tanımlı değil"}
                            onClick={() => onTestRowAgent(s)}
                          >
                            Agent testi
                          </button>
                          {canWrite && (
                            <>
                              <button className="btn" onClick={() => startEdit(s)}>Düzenle</button>
                              <button className="btn btn-danger" onClick={() => onDelete(s.id)}>Sil</button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                )
              )}
            </tbody>
          </table>
      </div>
    </>
  );
}
