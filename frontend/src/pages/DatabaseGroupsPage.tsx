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
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.getGroups(id).then(setGroups).catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    api.getApplication(id).then(setApplication).catch(() => undefined);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createGroup({ application_id: id, name, engine, topology, environment, notes: notes || undefined });
      setName("");
      setNotes("");
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

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Database Groups {application && <span className="detail-meta">— {application.name}</span>}</h2>
          <p>
            {application && <Link to={`/customers/${application.customer_id}/applications`}>← Uygulamalar</Link>}
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
                <th>Motor</th>
                <th>Topoloji</th>
                <th>Ortam</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {groups.length === 0 ? (
                <tr>
                  <td colSpan={5} className="empty">Kayıtlı grup yok</td>
                </tr>
              ) : (
                groups.map((g) => (
                  <tr key={g.id}>
                    <td>
                      <Link to={`/groups/${g.id}`}>{g.name}</Link>
                    </td>
                    <td>
                      <span className="engine-badge">{g.engine}</span>
                    </td>
                    <td>{g.topology}</td>
                    <td>
                      <span className={`env-badge ${g.environment}`}>{ENV_LABELS[g.environment]}</span>
                    </td>
                    <td>
                      <button className="btn btn-danger" onClick={() => onDelete(g.id)}>
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
          <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni database group</h3>
          <form className="form-grid" onSubmit={onSubmit}>
            <label>
              Ad
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="boa-sqlserver-ag" required />
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
