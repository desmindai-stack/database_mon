import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertEvent, AlertRule, api, DatabaseGroup, formatTime, Instance } from "../api";
import { useAuth } from "../auth";
import { TableState } from "../components/PageState";
import { useUrlTab } from "../hooks/useUrlState";

type Tab = "active" | "rules" | "history";
const TABS: readonly Tab[] = ["active", "rules", "history"];

const SEVERITIES = ["critical", "high", "warning", "medium", "low", "info"];
const ENGINES = ["postgresql", "sqlserver", "mongodb"];

export default function AlertsPage() {
  const canWrite = useAuth().user?.role === "admin";
  // Sekme URL'de (?tab=) — bkz. AdminPage'deki aynı düzeltme (Faz 19 İŞ 1).
  const [tab, setTab] = useUrlTab<Tab>("tab", TABS, "active");
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [activeEvents, setActiveEvents] = useState<AlertEvent[]>([]);
  const [historyEvents, setHistoryEvents] = useState<AlertEvent[]>([]);
  const [instances, setInstances] = useState<Instance[]>([]);
  const [groups, setGroups] = useState<DatabaseGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Uc tablonun da tek bir yuklemesi var: hata da bos da olsa ayni kaynaktan gelir.
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);

  const [search, setSearch] = useState("");
  const [severityFilter, setSeverityFilter] = useState("");
  const [engineFilter, setEngineFilter] = useState("");

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editThreshold, setEditThreshold] = useState<number>(0);
  const [editEnabled, setEditEnabled] = useState<boolean>(true);
  const [editSql, setEditSql] = useState<string>("");
  const [editIntervalSeconds, setEditIntervalSeconds] = useState<number>(60);
  const [editSeverity, setEditSeverity] = useState<string>("warning");

  const load = async () => {
    const [r, active, history, i, g] = await Promise.all([
      api.getAlertRules(),
      api.getAlertEvents(true),
      api.getAlertEvents(false),
      api.getInstances(),
      api.getGroups(),
    ]);
    setRules(r);
    setActiveEvents(active);
    setHistoryEvents(history.filter((e) => e.resolved_at !== null));
    setInstances(i);
    setGroups(g);
  };

  const reload = () =>
    load()
      .then(() => setListError(null))
      .catch(setListError)
      .finally(() => setLoaded(true));

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startEdit = (rule: AlertRule) => {
    setEditingId(rule.id);
    setEditThreshold(rule.threshold);
    setEditEnabled(rule.enabled);
    setEditSql(rule.sql_query || "");
    setEditIntervalSeconds(rule.interval_seconds);
    setEditSeverity(rule.severity);
  };

  const saveEdit = async (rule: AlertRule) => {
    const updates = rule.is_default
      ? { threshold: editThreshold, enabled: editEnabled }
      : {
          threshold: editThreshold,
          enabled: editEnabled,
          severity: editSeverity,
          interval_seconds: rule.rule_type === "custom" ? editIntervalSeconds : undefined,
          sql_query: rule.rule_type === "custom" ? editSql : undefined,
        };
    try {
      await api.updateAlertRule(rule.id, updates);
      setEditingId(null);
      await load();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const targetLabel = (rule: AlertRule) => {
    if (rule.instance_id) {
      const inst = instances.find((i) => i.id === rule.instance_id);
      return inst ? `Instance: ${inst.name}` : `Instance #${rule.instance_id}`;
    }
    if (rule.group_id) {
      const grp = groups.find((g) => g.id === rule.group_id);
      return grp ? `Grup: ${grp.name}` : `Grup #${rule.group_id}`;
    }
    return "Tümü";
  };

  const filteredRules = useMemo(() => {
    return rules.filter((rule) => {
      if (search.trim() && !rule.name.toLowerCase().includes(search.trim().toLowerCase())) return false;
      if (severityFilter && rule.severity !== severityFilter) return false;
      if (engineFilter && rule.engine !== engineFilter) return false;
      return true;
    });
  }, [rules, search, severityFilter, engineFilter]);

  const eventScope = (event: AlertEvent) =>
    event.instance_id ? (
      `Instance #${event.instance_id}`
    ) : event.group_id ? (
      <Link to={`/groups/${event.group_id}`}>Group #{event.group_id}</Link>
    ) : (
      "—"
    );

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Alerts</h2>
          <p>Varsayılan (otomatik) ve özel (SQL tabanlı) alarm kuralları</p>
        </div>
        {canWrite && (
          <div className="header-actions">
            <Link to="/alerts/new" className="btn btn-primary">+ Özel kural ekle</Link>
          </div>
        )}
      </header>

      {error && <div className="error">{error}</div>}

      <div className="detail-tabs">
        <button className={`tab-btn${tab === "active" ? " active" : ""}`} onClick={() => setTab("active")}>
          Aktif alarmlar {activeEvents.length > 0 && <span className="tag">{activeEvents.length}</span>}
        </button>
        <button className={`tab-btn${tab === "rules" ? " active" : ""}`} onClick={() => setTab("rules")}>
          Kural listesi
        </button>
        <button className={`tab-btn${tab === "history" ? " active" : ""}`} onClick={() => setTab("history")}>
          Geçmiş
        </button>
      </div>

      {tab === "active" && (
        <div className="table-wrap" style={{ marginTop: "1rem" }}>
          <table>
            <thead>
              <tr>
                <th>Ne zaman</th>
                <th>Kapsam</th>
                <th>Mesaj</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {activeEvents.length === 0 ? (
                <TableState
                  colSpan={4}
                  loading={!loaded}
                  error={listError}
                  onRetry={reload}
                  title="Aktif alarm yok"
                  detail="Şu anda eşiği aşan bir kural yok. Kurallar sekmesinden hangi eşiklerin izlendiğini görebilirsiniz."
                />
              ) : (
                activeEvents.map((event) => (
                  <tr key={event.id}>
                    <td>{formatTime(event.triggered_at)}</td>
                    <td>{eventScope(event)}</td>
                    <td>{event.message}</td>
                    <td>
                      {canWrite && (
                        <button className="btn" onClick={() => api.resolveAlert(event.id).then(load)}>
                          Resolve
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === "rules" && (
        <>
          <div className="activity-toolbar" style={{ marginTop: "1rem" }}>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Kural adında ara…"
              style={{ maxWidth: 240 }}
            />
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <select value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
                <option value="">Tüm önem dereceleri</option>
                {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <select value={engineFilter} onChange={(e) => setEngineFilter(e.target.value)}>
                <option value="">Tüm engine'ler</option>
                {ENGINES.map((eng) => <option key={eng} value={eng}>{eng}</option>)}
              </select>
            </div>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Kural</th>
                  <th>Tip</th>
                  <th>Koşul</th>
                  <th>Hedef</th>
                  <th>Durum</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filteredRules.length === 0 ? (
                  <TableState
                    colSpan={6}
                    loading={!loaded}
                    error={listError}
                    onRetry={reload}
                    title={rules.length === 0 ? "Tanımlı alarm kuralı yok" : "Filtreye uyan kural yok"}
                    detail={
                      rules.length === 0
                        ? "Varsayılan kurallar ilk toplama döngüsünde oluşturulur; özel bir kuralı sağ üstteki düğmeyle ekleyebilirsiniz."
                        : "Filtreyi genişletin ya da temizleyin."
                    }
                  />
                ) : (
                  filteredRules.map((rule) => (
                    <tr key={rule.id}>
                      <td>
                        {rule.name}
                        <span className={`tag ${rule.is_default ? "public" : "private"}`} style={{ marginLeft: "0.4rem" }}>
                          {rule.is_default ? "Varsayılan" : "Özel"}
                        </span>
                      </td>
                      <td>{rule.rule_type === "custom" ? "Özel SQL" : "Metrik"}</td>
                      <td>
                        {editingId === rule.id ? (
                          <input
                            type="number"
                            style={{ width: "5rem" }}
                            value={editThreshold}
                            onChange={(e) => setEditThreshold(Number(e.target.value))}
                          />
                        ) : (
                          <code>
                            {rule.rule_type === "custom" ? "sorgu sonucu" : rule.metric} {rule.operator} {rule.threshold}
                          </code>
                        )}
                      </td>
                      <td>{targetLabel(rule)}</td>
                      <td>
                        {editingId === rule.id ? (
                          <label>
                            <input type="checkbox" checked={editEnabled} onChange={(e) => setEditEnabled(e.target.checked)} /> aktif
                          </label>
                        ) : (
                          <span className={`status ${rule.enabled ? "healthy" : "disabled"}`}>{rule.enabled ? "aktif" : "kapalı"}</span>
                        )}
                      </td>
                      <td>
                        {!canWrite ? null : editingId === rule.id ? (
                          <div style={{ display: "flex", gap: "0.3rem" }}>
                            <button className="btn btn-primary" onClick={() => saveEdit(rule)}>Kaydet</button>
                            <button className="btn" onClick={() => setEditingId(null)}>Vazgeç</button>
                          </div>
                        ) : (
                          <div style={{ display: "flex", gap: "0.3rem" }}>
                            <button className="btn" onClick={() => startEdit(rule)}>Düzenle</button>
                            {!rule.is_default && (
                              <button className="btn btn-danger" onClick={() => api.deleteAlertRule(rule.id).then(load)}>
                                Sil
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "history" && (
        <div className="table-wrap" style={{ marginTop: "1rem" }}>
          <table>
            <thead>
              <tr>
                <th>Tetiklendi</th>
                <th>Çözüldü</th>
                <th>Kapsam</th>
                <th>Mesaj</th>
              </tr>
            </thead>
            <tbody>
              {historyEvents.length === 0 ? (
                <TableState
                  colSpan={4}
                  loading={!loaded}
                  error={listError}
                  onRetry={reload}
                  title="Geçmişte çözülmüş alarm yok"
                  detail="Bir alarm çözüldüğünde kaydı buraya taşınır; saklama süresi Yönetim → Saklama ayarından belirlenir."
                />
              ) : (
                historyEvents.map((event) => (
                  <tr key={event.id}>
                    <td>{formatTime(event.triggered_at)}</td>
                    <td>{event.resolved_at ? formatTime(event.resolved_at) : "—"}</td>
                    <td>{eventScope(event)}</td>
                    <td>{event.message}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
