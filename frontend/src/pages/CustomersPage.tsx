import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, Customer, CustomerType, errorMessage } from "../api";
import { TableState } from "../components/PageState";
import { useAuth } from "../auth";

export default function CustomersPage() {
  const navigate = useNavigate();
  const canWrite = useAuth().user?.role === "admin";
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [isPrivate, setIsPrivate] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<CustomerType>("private");
  const [error, setError] = useState<string | null>(null);
  // Yukleme HATASI ile "gercekten bos" ayri seyler: eskiden ikisi de "Kayitli musteri
  // yok" satirini gosteriyordu, yani API dustugunde kullanici kayit olmadigina inaniyordu.
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editType, setEditType] = useState<CustomerType>("private");

  const load = () =>
    api
      .getCustomers()
      .then((rows) => {
        setCustomers(rows);
        setListError(null);
      })
      .catch(setListError)
      .finally(() => setLoaded(true));

  useEffect(() => {
    api.getConfig().then((cfg) => {
      if (cfg.deployment_mode === "private") {
        setIsPrivate(true);
        api.getCustomers().then((all) => {
          if (all.length > 0) navigate(`/customers/${all[0].id}/applications`, { replace: true });
        }).catch(() => undefined);
      }
    }).catch(() => undefined);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createCustomer({ name, type });
      setName("");
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (id: number) => {
    if (!confirm("Müşteri ve altındaki tüm uygulama/grup/düğüm kayıtları silinsin mi?")) return;
    // Eskiden `catch` yoktu: silme reddedilirse (baska sekmede zaten silinmis, sunucu hatasi,
    // ag kopmasi) kullaniciya HICBIR SEY soylenmiyor, satir yerinde kaliyordu (Faz 19 IS 2).
    setError(null);
    try {
      await api.deleteCustomer(id);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const startEdit = (c: Customer) => {
    setEditingId(c.id);
    setEditName(c.name);
    setEditType(c.type);
  };

  const saveEdit = async (id: number) => {
    try {
      await api.updateCustomer(id, { name: editName, type: editType });
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
          <h2>Müşteriler</h2>
          <p>Customer → Application → Database Group → Node hiyerarşisinin kökü</p>
        </div>
        {!isPrivate && canWrite && (
          <div className="header-actions">
            <a href="#new-customer-form" className="btn btn-primary">+ Müşteri Ekle</a>
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
                <th>Tip</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {customers.length === 0 ? (
                <TableState
                  colSpan={3}
                  loading={!loaded}
                  error={listError}
                  onRetry={load}
                  title="Kayıtlı müşteri yok"
                  detail={
                    canWrite
                      ? "İzlenecek veritabanları müşteri altında gruplanır. Aşağıdaki formdan ilk müşteriyi ekleyin."
                      : "İzlenecek veritabanları müşteri altında gruplanır. Müşteri eklemek admin yetkisi gerektirir."
                  }
                />
              ) : (
                customers.map((c) => (
                  <tr key={c.id}>
                    {editingId === c.id ? (
                      <>
                        <td>
                          <input value={editName} onChange={(e) => setEditName(e.target.value)} />
                        </td>
                        <td>
                          <select value={editType} onChange={(e) => setEditType(e.target.value as CustomerType)}>
                            <option value="private">Private</option>
                            <option value="public">Public</option>
                          </select>
                        </td>
                        <td>
                          <div style={{ display: "flex", gap: "0.3rem" }}>
                            <button className="btn btn-primary" onClick={() => saveEdit(c.id)}>Kaydet</button>
                            <button className="btn" onClick={() => setEditingId(null)}>Vazgeç</button>
                          </div>
                        </td>
                      </>
                    ) : (
                      <>
                        <td>
                          <Link to={`/customers/${c.id}/applications`}>{c.name}</Link>
                        </td>
                        <td>
                          <span className={`tag ${c.type}`}>{c.type}</span>
                        </td>
                        <td>
                          {!isPrivate && canWrite && (
                            <div style={{ display: "flex", gap: "0.3rem" }}>
                              <button className="btn" onClick={() => startEdit(c)}>Düzenle</button>
                              <button className="btn btn-danger" onClick={() => onDelete(c.id)}>Sil</button>
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

        {!isPrivate && canWrite && (
          <div className="card" id="new-customer-form">
            <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni müşteri</h3>
            <form className="form-grid" onSubmit={onSubmit}>
              <label>
                Ad
                <input value={name} onChange={(e) => setName(e.target.value)} required />
              </label>
              <label>
                Tip
                <select value={type} onChange={(e) => setType(e.target.value as CustomerType)}>
                  <option value="private">Private</option>
                  <option value="public">Public</option>
                </select>
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
