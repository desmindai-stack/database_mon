import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, Customer, CustomerType } from "../api";

export default function CustomersPage() {
  const navigate = useNavigate();
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [isPrivate, setIsPrivate] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<CustomerType>("private");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.getCustomers().then(setCustomers).catch((e) => setError(String(e.message || e)));

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
    await api.deleteCustomer(id);
    await load();
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Müşteriler</h2>
          <p>Customer → Application → Database Group → Node hiyerarşisinin kökü</p>
        </div>
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
                <tr>
                  <td colSpan={3} className="empty">Kayıtlı müşteri yok</td>
                </tr>
              ) : (
                customers.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <Link to={`/customers/${c.id}/applications`}>{c.name}</Link>
                    </td>
                    <td>
                      <span className={`tag ${c.type}`}>{c.type}</span>
                    </td>
                    <td>
                      {!isPrivate && (
                        <button className="btn btn-danger" onClick={() => onDelete(c.id)}>
                          Sil
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {!isPrivate && (
          <div className="card">
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
