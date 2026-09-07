import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Application, Customer, errorMessage } from "../api";
import { NotFoundState, PageError, PageSkeleton, TableState } from "../components/PageState";
import { useAuth } from "../auth";

export default function ApplicationsPage() {
  const { customerId } = useParams<{ customerId: string }>();
  const id = Number(customerId);
  const idIsValid = Number.isInteger(id) && id > 0;
  const canWrite = useAuth().user?.role === "admin";

  const [customer, setCustomer] = useState<Customer | null>(null);
  const [applications, setApplications] = useState<Application[]>([]);
  const [isPrivate, setIsPrivate] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  const load = () =>
    api
      .getApplications(id)
      .then((rows) => {
        setApplications(rows);
        setListError(null);
      })
      .catch(setListError)
      .finally(() => setLoaded(true));

  useEffect(() => {
    if (!idIsValid) return;
    api.getConfig().then((cfg) => setIsPrivate(cfg.deployment_mode === "private")).catch(() => undefined);
    // Bu cagrinin hic `catch`'i yoktu — API dustugunde konsola yakalanmamis bir promise reddi
    // dusuyor, sayfa sessizce musteri adi olmadan aciliyordu. Ayrica listede olmayan bir
    // musteri numarasi "bulunamadi" olarak ele alinmiyordu (Faz 19 IS 1).
    api.getCustomers().then((all) => {
      const found = all.find((c) => c.id === id) ?? null;
      setCustomer(found);
      setNotFound(found === null);
    }).catch((e) => setError(String((e as Error).message || e)));
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, idIsValid, reloadKey]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createApplication({ customer_id: id, name, description: description || undefined });
      setName("");
      setDescription("");
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (appId: number) => {
    if (!confirm("Uygulama ve altındaki tüm grup/düğüm kayıtları silinsin mi?")) return;
    // Sessiz basarisizlik duzeltildi (Faz 19 IS 2) — bkz. CustomersPage'deki ayni not.
    setError(null);
    try {
      await api.deleteApplication(appId);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const startEdit = (a: Application) => {
    setEditingId(a.id);
    setEditName(a.name);
    setEditDescription(a.description || "");
  };

  const saveEdit = async (appId: number) => {
    try {
      await api.updateApplication(appId, { name: editName, description: editDescription || undefined });
      setEditingId(null);
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
    return error ? <PageError error={error} onRetry={() => setReloadKey((k) => k + 1)} /> : <PageSkeleton rows={4} />;
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Uygulamalar {customer && <span className="detail-meta">— {customer.name}</span>}</h2>
          {!isPrivate && (
            <p>
              <Link to="/customers">← Müşteriler</Link>
            </p>
          )}
        </div>
        <div className="header-actions">
          <Link to={`/customers/${id}/servers`} className="btn">Sunucular</Link>
          {canWrite && <a href="#new-application-form" className="btn btn-primary">+ Uygulama Ekle</a>}
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="grid grid-2">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ad</th>
                <th>Açıklama</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {applications.length === 0 ? (
                <TableState
                  colSpan={3}
                  loading={!loaded}
                  error={listError}
                  onRetry={load}
                  title="Kayıtlı uygulama yok"
                  detail={
                    canWrite
                      ? "Uygulama, veritabanı gruplarını bir arada tutar (örn. \u201cboa\u201d → SQL Server Always On). Aşağıdaki formdan ilkini ekleyin."
                      : "Uygulama, veritabanı gruplarını bir arada tutar. Uygulama eklemek admin yetkisi gerektirir."
                  }
                />
              ) : (
                applications.map((a) => (
                  <tr key={a.id}>
                    {editingId === a.id ? (
                      <>
                        <td>
                          <input value={editName} onChange={(e) => setEditName(e.target.value)} />
                        </td>
                        <td>
                          <input value={editDescription} onChange={(e) => setEditDescription(e.target.value)} />
                        </td>
                        <td>
                          <div style={{ display: "flex", gap: "0.25rem" }}>
                            <button className="btn btn-primary" onClick={() => saveEdit(a.id)}>Kaydet</button>
                            <button className="btn" onClick={() => setEditingId(null)}>Vazgeç</button>
                          </div>
                        </td>
                      </>
                    ) : (
                      <>
                        <td>
                          <Link to={`/applications/${a.id}/groups`}>{a.name}</Link>
                        </td>
                        <td>{a.description || "—"}</td>
                        <td>
                          {canWrite && (
                            <div style={{ display: "flex", gap: "0.25rem" }}>
                              <button className="btn" onClick={() => startEdit(a)}>Düzenle</button>
                              <button className="btn btn-danger" onClick={() => onDelete(a.id)}>Sil</button>
                            </div>
                          )}
                        </td>
                      </>
                    )}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {canWrite && (
          <div className="card" id="new-application-form">
            <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni uygulama</h3>
            <form className="form-grid" onSubmit={onSubmit}>
              <label>
                Ad
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="boa, aapara..." required />
              </label>
              <label>
                Açıklama
                <input value={description} onChange={(e) => setDescription(e.target.value)} />
              </label>
              <div className="form-actions">
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  Ekle
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    </>
  );
}
