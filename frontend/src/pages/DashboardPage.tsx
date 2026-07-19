import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, formatBytes, HealthResponse, InstanceSummary } from "../api";

type StatusFilter = "all" | "healthy" | "warning" | "alerting" | "pending" | "disabled";
type EnvFilter = "all" | "public" | "private";

const statusMeta: Record<string, { label: string; color: string }> = {
  healthy: { label: "Healthy", color: "var(--success)" },
  warning: { label: "Warning", color: "var(--warning)" },
  alerting: { label: "Alerting", color: "var(--danger)" },
  pending: { label: "Pending", color: "var(--muted)" },
  disabled: { label: "Disabled", color: "var(--muted)" },
};

function connectionUtilPct(s: InstanceSummary): number {
  const m = s.latest_metrics;
  if (!m || !m.max_connections) return 0;
  return Math.min(100, (m.active_connections / m.max_connections) * 100);
}

function severityRank(s: InstanceSummary): number {
  const order = { alerting: 0, warning: 1, pending: 2, healthy: 3, disabled: 4 };
  return order[s.status as keyof typeof order] ?? 5;
}

export default function DashboardPage() {
  const [summaries, setSummaries] = useState<InstanceSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<HealthResponse | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [envFilter, setEnvFilter] = useState<EnvFilter>("all");
  const [searchParams, setSearchParams] = useSearchParams();
  const customerFilter = searchParams.get("customer");
  const appFilter = searchParams.get("app");

  useEffect(() => {
    api.getSummaries()
      .then(setSummaries)
      .catch((err) => setError(String(err.message || err)));
    api.getHealth().then(setConfig).catch(() => undefined);
  }, []);

  const totalConnections = summaries.reduce(
    (sum, s) => sum + (s.latest_metrics?.active_connections ?? 0),
    0,
  );
  const counts = useMemo(() => {
    const c: Record<string, number> = { healthy: 0, warning: 0, alerting: 0, pending: 0, disabled: 0 };
    for (const s of summaries) c[s.status] = (c[s.status] ?? 0) + 1;
    return c;
  }, [summaries]);

  const isPrivate = config?.deployment_mode === "private";

  const filtered = useMemo(() => {
    let list = summaries;
    if (statusFilter !== "all") list = list.filter((s) => s.status === statusFilter);
    if (envFilter !== "all") list = list.filter((s) => s.instance.environment === envFilter);
    if (customerFilter) list = list.filter((s) => (s.instance.customer_name || "Bilinmeyen Müşteri") === customerFilter);
    if (appFilter) list = list.filter((s) => s.instance.application === appFilter);
    return list.sort((a, b) => severityRank(a) - severityRank(b));
  }, [summaries, statusFilter, envFilter, customerFilter, appFilter]);

  const grouped = useMemo(() => {
    const map = new Map<string, InstanceSummary[]>();
    for (const s of filtered) {
      let key: string;
      if (appFilter) {
        // Within a selected app, group by cluster
        const cluster = s.instance.cluster_name || "Tekil sunucular";
        key = `${appFilter} / ${cluster}`;
      } else if (customerFilter) {
        // Within a selected customer, group by application/cluster
        key = `${s.instance.application || "Uygulamasız"}${s.instance.cluster_name ? " / " + s.instance.cluster_name : ""}`;
      } else if (isPrivate) {
        key = `${s.instance.application || "Uygulama"}${s.instance.cluster_name ? " / " + s.instance.cluster_name : ""}`;
      } else {
        key = `${s.instance.customer_name || "Bilinmeyen Müşteri"} / ${s.instance.application || "—"}`;
      }
      const list = map.get(key) ?? [];
      list.push(s);
      map.set(key, list);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [filtered, isPrivate, customerFilter, appFilter]);

  const StatCard = ({
    label,
    value,
    color,
    sub,
    active,
    onClick,
  }: {
    label: string;
    value: string | number;
    color?: string;
    sub?: string;
    active?: boolean;
    onClick?: () => void;
  }) => {
    const card = (
      <div
        className={`card stat-card${onClick ? " clickable" : ""}${active ? " active" : ""}`}
        style={{ borderLeftColor: color || "var(--accent)" }}
      >
        <div className="stat-meta">
          <h3>{label}</h3>
          {sub && <span>{sub}</span>}
        </div>
        <div className="value" style={{ color: color || "var(--text)" }}>{value}</div>
      </div>
    );
    if (!onClick) return card;
    return (
      <button className="stat-card-btn" onClick={onClick} type="button">
        {card}
      </button>
    );
  };

  const FilterChip = ({ active, label, onClick, color }: { active: boolean; label: string; onClick: () => void; color?: string }) => (
    <button
      className={`filter-chip${active ? " active" : ""}`}
      onClick={onClick}
      style={active && color ? { borderColor: color, color } : undefined}
    >
      {label}
    </button>
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h2>DBA Overview</h2>
          <p>
            {isPrivate
              ? `${config?.default_customer_name || "Private"} ortamı — tüm veritabanı instance’larının özeti`
              : "Tüm müşteri ve uygulamaların veritabanı sağlık özeti"}
          </p>
        </div>
        <div className="header-actions">
          <Link to="/instances" className="btn">Tüm instance’lar</Link>
          <Link to="/instances" className="btn btn-primary">+ Yeni instance</Link>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="stats-grid">
        <StatCard label="Toplam instance" value={summaries.length} color="var(--accent)" sub="monitor ediliyor" />
        <StatCard label="Aktif bağlantı" value={totalConnections} color="#22d3ee" sub="tüm sunucular" />
        <StatCard label="Healthy" value={counts.healthy} color="var(--success)" active={statusFilter === "healthy"} onClick={() => setStatusFilter("healthy")} />
        <StatCard label="Alerting" value={counts.alerting} color="var(--danger)" active={statusFilter === "alerting"} onClick={() => setStatusFilter("alerting")} />
        <StatCard label="Warning" value={counts.warning} color="var(--warning)" active={statusFilter === "warning"} onClick={() => setStatusFilter("warning")} />
        <StatCard label="Açık tahmin" value={summaries.reduce((sum, s) => sum + (s.predictions_open ?? 0), 0)} color="#a78bfa" />
      </div>

      <div className="filters-bar">
        <div className="filter-group">
          <span className="filter-label">Durum</span>
          {(["all", "alerting", "warning", "pending", "healthy", "disabled"] as StatusFilter[]).map((st) => (
            <FilterChip
              key={st}
              active={statusFilter === st}
              label={st === "all" ? "Tümü" : `${statusMeta[st].label} ${counts[st] ? `(${counts[st]})` : ""}`}
              onClick={() => setStatusFilter(st)}
              color={st !== "all" ? statusMeta[st].color : undefined}
            />
          ))}
        </div>
        <div className="filter-group">
          <span className="filter-label">Ortam</span>
          {(["all", "public", "private"] as EnvFilter[]).map((env) => (
            <FilterChip
              key={env}
              active={envFilter === env}
              label={env === "all" ? "Tümü" : env === "public" ? "Public" : "Private"}
              onClick={() => setEnvFilter(env)}
            />
          ))}
        </div>
        {(customerFilter || appFilter) && (
          <div className="filter-group active-filter">
            <span className="filter-label">Aktif filtre</span>
            <span className="filter-chip active">
              {customerFilter || ""} {customerFilter && appFilter ? "/" : ""} {appFilter || ""}
            </span>
            <button
              className="filter-chip"
              onClick={() => setSearchParams({})}
            >
              Temizle
            </button>
          </div>
        )}
      </div>

      {grouped.length === 0 ? (
        <div className="card empty-card">
          <p style={{ color: "var(--muted)" }}>
            Filtrelere uygun instance yok. <Link to="/instances">Yeni instance ekle</Link>.
          </p>
        </div>
      ) : (
        grouped.map(([group, items]) => (
          <div className="card group-card" key={group}>
            <div className="group-header">
              <h3>{group}</h3>
              <span className="group-count">{items.length} instance</span>
            </div>
            <div className="table-wrap">
              <table className="dashboard-table">
                <thead>
                  <tr>
                    <th>Instance</th>
                    {!isPrivate && <th>Müşteri</th>}
                    <th>Ortam</th>
                    <th>Cluster / Rol</th>
                    <th>Motor</th>
                    <th>Durum</th>
                    <th>Bağlantı</th>
                    <th>Cache hit</th>
                    <th>TPS</th>
                    <th>I/O okuma/ hit</th>
                    <th>Temp</th>
                    <th>DB boyutu</th>
                    <th>Uyarı</th>
                    <th>Detay</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((summary) => {
                    const { instance, latest_metrics, status, alerts_firing } = summary;
                    const util = connectionUtilPct(summary);
                    const m = latest_metrics?.metrics ?? {};
                    return (
                      <tr key={instance.id}>
                        <td>
                          <Link
                            to={instance.application ? `/?app=${encodeURIComponent(instance.application)}` : `/instances/${instance.id}`}
                            className="instance-name"
                            title="Uygulama filtresine git"
                          >
                            {instance.name}
                          </Link>
                          <div className="instance-meta">
                            {instance.host}:{instance.port}/{instance.database}
                          </div>
                        </td>
                        {!isPrivate && <td>{instance.customer_name || "—"}</td>}
                        <td><span className={`env-badge ${instance.environment}`}>{instance.environment}</span></td>
                        <td>
                          {instance.cluster_name || "—"}
                          {instance.role && <div className="role-tag">{instance.role}</div>}
                        </td>
                        <td><span className="engine-badge">{instance.engine}</span></td>
                        <td><span className={`status ${status}`}>{status}</span></td>
                        <td>
                          {latest_metrics ? (
                            <div className="util-cell">
                              <span>{latest_metrics.active_connections} / {latest_metrics.max_connections}</span>
                              <div className="util-bar">
                                <div className="util-fill" style={{ width: `${util}%`, background: util > 85 ? "var(--danger)" : util > 60 ? "var(--warning)" : "var(--success)" }} />
                              </div>
                            </div>
                          ) : "—"}
                        </td>
                        <td>
                          {latest_metrics ? (
                            <div className="util-cell">
                              <span>{latest_metrics.cache_hit_ratio.toFixed(1)}%</span>
                              <div className="util-bar">
                                <div
                                  className="util-fill"
                                  style={{
                                    width: `${latest_metrics.cache_hit_ratio}%`,
                                    background: latest_metrics.cache_hit_ratio < 95 ? "var(--warning)" : "var(--success)",
                                  }}
                                />
                              </div>
                            </div>
                          ) : "—"}
                        </td>
                        <td>{latest_metrics ? latest_metrics.transactions_per_sec.toFixed(1) : "—"}</td>
                        <td>
                          {latest_metrics ? (
                            <span>
                              {Number(m.blks_read_per_sec ?? 0).toFixed(0)} / {Number(m.blks_hit_per_sec ?? 0).toFixed(0)}
                            </span>
                          ) : "—"}
                        </td>
                        <td>
                          {latest_metrics ? (
                            <span>
                              {Number(m.temp_files_per_sec ?? 0).toFixed(1)} f, {formatBytes(Number(m.temp_bytes_per_sec ?? 0))}/s
                            </span>
                          ) : "—"}
                        </td>
                        <td>{latest_metrics ? formatBytes(latest_metrics.database_size_bytes) : "—"}</td>
                        <td>{alerts_firing ? <span className="alert-count">{alerts_firing}</span> : "—"}</td>
                        <td>
                          <Link to={`/instances/${instance.id}`} className="detail-link">detay</Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </>
  );
}
