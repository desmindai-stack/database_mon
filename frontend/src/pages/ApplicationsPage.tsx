import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Application, Customer } from "../api";

export default function ApplicationsPage() {
  const { customerId } = useParams<{ customerId: string }>();
  const id = Number(customerId);

  const [customer, setCustomer] = useState<Customer | null>(null);
  const [applications, setApplications] = useState<Application[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.getApplications(id).then(setApplications).catch((e) => setError(String(e.message || e)));

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
    await api.deleteApplication(appId);
    await load();
  };

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Uygulamalar {customer && <span className="detail-meta">— {customer.name}</span>}</h2>
          <p>
            <Link to="/customers">← Müşteriler</Link>
          </p>
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
                <tr>
                  <td colSpan={3} className="empty">Kayıtlı uygulama yok</td>
                </tr>
              ) : (
                applications.map((a) => (
                  <tr key={a.id}>
                    <td>
                      <Link to={`/applications/${a.id}/groups`}>{a.name}</Link>
                    </td>
                    <td>{a.description || "—"}</td>
                    <td>
                      <button className="btn btn-danger" onClick={() => onDelete(a.id)}>
                        Sil
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
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
      </div>
    </>
  );
}
