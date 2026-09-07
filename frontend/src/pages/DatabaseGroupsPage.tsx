import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, Application, DatabaseGroup, GroupEnvironment } from "../api";
import { NotFoundState, PageError, PageLoading } from "../components/PageState";
import { useAuth } from "../auth";

const ENV_LABELS: Record<GroupEnvironment, string> = {
  prod: "Prod",
  preprod: "Preprod",
  test: "Test",
  dev: "Dev",
};

export default function DatabaseGroupsPage() {
  const { applicationId } = useParams<{ applicationId: string }>();
  const id = Number(applicationId);
  const idIsValid = Number.isInteger(id) && id > 0;
  const canWrite = useAuth().user?.role === "admin";

  const [application, setApplication] = useState<Application | null>(null);
  const [groups, setGroups] = useState<DatabaseGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editEnvironment, setEditEnvironment] = useState<GroupEnvironment>("prod");
  const [editAccessName, setEditAccessName] = useState("");
  const [editNotes, setEditNotes] = useState("");

  const load = () => api.getGroups(id).then(setGroups).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    if (!idIsValid) return;
    // Eskiden bu cagrinin hatasi SESSIZCE yutuluyordu: silinmis bir uygulamaya gidildiginde
    // sayfa aciliyor ama basliktaki uygulama adi ve "← Uygulamalar" geri baglantisi hic
    // gorunmuyordu — kullanici cikamadigi bos bir sayfada kaliyordu (Faz 19 IS 1).
    api.getApplication(id).then((a) => {
      setApplication(a);
      setNotFound(false);
    }).catch((e) => {
      if (e instanceof ApiError && e.isNotFound) setNotFound(true);
      else setError(String(e.message || e));
    });
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, idIsValid, reloadKey]);

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

  if (!idIsValid || notFound) {
    return (
      <NotFoundState
        title="Uygulama bulunamadı"
        detail={
          idIsValid
            ? `#${id} numaralı uygulama yok — silinmiş olabilir ya da bağlantı eskimiş olabilir.`
            : `"${applicationId}" geçerli bir uygulama numarası değil.`
        }
        backTo="/customers"
        backLabel="Müşteri listesine dön"
      />
    );
  }

  if (!application) {
    return error ? <PageError error={error} onRetry={() => setReloadKey((k) => k + 1)} /> : <PageLoading />;
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Database Groups {application && <span className="detail-meta">— {application.name}</span>}</h2>
          <p>
            {application && <Link to={`/customers/${application.customer_id}/applications`}>← Uygulamalar</Link>}
          </p>
        </div>
        {canWrite && (
          <div className="header-actions">
            <Link to={`/applications/${id}/groups/wizard`} className="btn btn-primary">+ Veritabanı Ekle</Link>
          </div>
        )}
      </header>

      {error && <div className="error">{error}</div>}

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
                        {canWrite && (
                          <div style={{ display: "flex", gap: "0.3rem" }}>
                            <button className="btn" onClick={() => startEdit(g)}>Düzenle</button>
                            <button className="btn btn-danger" onClick={() => onDelete(g.id)}>Sil</button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
      </div>
    </>
  );
}
