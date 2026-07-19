import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, Route, Routes, useSearchParams } from "react-router-dom";
import { api, InstanceSummary } from "./api";
import AlertsPage from "./pages/AlertsPage";
import DashboardPage from "./pages/DashboardPage";
import InstanceDetailPage from "./pages/InstanceDetailPage";
import InstancesPage from "./pages/InstancesPage";
import PredictionsPage from "./pages/PredictionsPage";

type AppNode = {
  name: string;
  instances: { id: number; name: string; cluster_name: string | null }[];
};

type CustomerNode = {
  name: string;
  apps: AppNode[];
};

function CustomerTree() {
  const [summaries, setSummaries] = useState<InstanceSummary[]>([]);
  const [openCustomers, setOpenCustomers] = useState<Set<string>>(new Set());
  const [openApps, setOpenApps] = useState<Set<string>>(new Set());
  const [isOpen, setIsOpen] = useState(false);
  const [searchParams] = useSearchParams();
  const activeCustomer = searchParams.get("customer");
  const activeApp = searchParams.get("app");

  useEffect(() => {
    api.getSummaries().then(setSummaries).catch(() => undefined);
  }, []);

  const tree = useMemo<CustomerNode[]>(() => {
    const customerMap = new Map<string, Map<string, AppNode>>();
    for (const s of summaries) {
      const customer = s.instance.customer_name || "Bilinmeyen Müşteri";
      const app = s.instance.application || "Uygulamasız";
      if (!customerMap.has(customer)) customerMap.set(customer, new Map());
      const appMap = customerMap.get(customer)!;
      if (!appMap.has(app)) {
        appMap.set(app, { name: app, instances: [] });
      }
      appMap.get(app)!.instances.push({
        id: s.instance.id,
        name: s.instance.name,
        cluster_name: s.instance.cluster_name,
      });
    }
    const result: CustomerNode[] = [];
    for (const [customer, apps] of customerMap.entries()) {
      const appList = Array.from(apps.values()).sort((a, b) => a.name.localeCompare(b.name));
      appList.forEach((app) => {
        app.instances.sort((a, b) => a.name.localeCompare(b.name));
      });
      result.push({ name: customer, apps: appList });
    }
    return result.sort((a, b) => a.name.localeCompare(b.name));
  }, [summaries]);

  const toggleCustomer = (customer: string) => {
    setOpenCustomers((prev) => {
      const next = new Set(prev);
      if (next.has(customer)) next.delete(customer);
      else next.add(customer);
      return next;
    });
  };

  const toggleApp = (app: string) => {
    setOpenApps((prev) => {
      const next = new Set(prev);
      if (next.has(app)) next.delete(app);
      else next.add(app);
      return next;
    });
  };

  const isActive = activeCustomer !== null || activeApp !== null;

  return (
    <div className="nav-group">
      <button
        className={`nav-link nav-tree-root${isActive ? " active" : ""}${isOpen ? " open" : ""}`}
        onClick={() => setIsOpen(!isOpen)}
      >
        <span>Müşteriler</span>
        <span className="nav-tree-chevron">{isOpen ? "▾" : "▸"}</span>
      </button>
      {isOpen && (
        <div className="nav-tree">
          {tree.map((customer) => {
            const isCustomerOpen = openCustomers.has(customer.name) || (activeCustomer === customer.name && activeApp !== null);
            return (
              <div key={customer.name} className="nav-tree-section">
                <button className="nav-tree-customer" onClick={() => toggleCustomer(customer.name)}>
                  <span className={`nav-tree-arrow${isCustomerOpen ? " open" : ""}`}>▶</span>
                  {customer.name}
                </button>
                {isCustomerOpen && (
                  <div className="nav-tree-apps">
                    {customer.apps.map((app) => {
                      const isAppOpen = openApps.has(app.name) || (activeCustomer === customer.name && activeApp === app.name);
                      const appActive = activeCustomer === customer.name && activeApp === app.name;
                      return (
                        <div key={app.name} className="nav-tree-app-section">
                          <div className="nav-tree-app-row">
                            <Link
                              to={`/?customer=${encodeURIComponent(customer.name)}&app=${encodeURIComponent(app.name)}`}
                              className={`nav-tree-app${appActive ? " active" : ""}`}
                            >
                              {app.name}
                            </Link>
                            <button
                              className={`nav-tree-app-toggle${isAppOpen ? " open" : ""}`}
                              onClick={() => toggleApp(app.name)}
                            >
                              ▶
                            </button>
                          </div>
                          {isAppOpen && (
                            <div className="nav-tree-instances">
                              {app.instances.map((inst) => (
                                <span
                                  key={inst.id}
                                  className="nav-tree-instance"
                                  title={inst.cluster_name || undefined}
                                >
                                  {inst.name}
                                  {inst.cluster_name && <span className="nav-tree-cluster">{inst.cluster_name}</span>}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">DB</div>
          <div>
            <h1>pgwatch</h1>
            <p>DBA monitoring platform</p>
          </div>
        </div>
        <nav>
          <NavLink to="/" end className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Dashboard
          </NavLink>
          <CustomerTree />
          <NavLink to="/instances" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Instances
          </NavLink>
          <NavLink to="/predictions" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Predictions
          </NavLink>
          <NavLink to="/alerts" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Alerts
          </NavLink>
        </nav>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/instances" element={<InstancesPage />} />
          <Route path="/instances/:id" element={<InstanceDetailPage />} />
          <Route path="/predictions" element={<PredictionsPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
        </Routes>
      </main>
    </div>
  );
}
