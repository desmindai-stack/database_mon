import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, Route, Routes, useSearchParams } from "react-router-dom";
import { api, InstanceSummary } from "./api";
import AlertsPage from "./pages/AlertsPage";
import DashboardPage from "./pages/DashboardPage";
import InstanceDetailPage from "./pages/InstanceDetailPage";
import InstancesPage from "./pages/InstancesPage";
import PredictionsPage from "./pages/PredictionsPage";

function CustomerTree() {
  const [summaries, setSummaries] = useState<InstanceSummary[]>([]);
  const [openCustomers, setOpenCustomers] = useState<Set<string>>(new Set());
  const [isOpen, setIsOpen] = useState(false);
  const [searchParams] = useSearchParams();
  const activeCustomer = searchParams.get("customer");
  const activeApp = searchParams.get("app");

  useEffect(() => {
    api.getSummaries().then(setSummaries).catch(() => undefined);
  }, []);

  const tree = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const s of summaries) {
      const customer = s.instance.customer_name || "Bilinmeyen Müşteri";
      const app = s.instance.application || "Uygulamasız";
      const apps = map.get(customer) ?? new Set<string>();
      apps.add(app);
      map.set(customer, apps);
    }
    const result: [string, string[]][] = [];
    for (const [customer, apps] of map.entries()) {
      result.push([customer, Array.from(apps).sort((a, b) => a.localeCompare(b))]);
    }
    return result.sort((a, b) => a[0].localeCompare(b[0]));
  }, [summaries]);

  const toggleCustomer = (customer: string) => {
    setOpenCustomers((prev) => {
      const next = new Set(prev);
      if (next.has(customer)) next.delete(customer);
      else next.add(customer);
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
          {tree.map(([customer, apps]) => {
            const isCustomerOpen = openCustomers.has(customer) || (activeCustomer === customer && activeApp !== null);
            return (
              <div key={customer} className="nav-tree-section">
                <button className="nav-tree-customer" onClick={() => toggleCustomer(customer)}>
                  <span className={`nav-tree-arrow${isCustomerOpen ? " open" : ""}`}>▶</span>
                  {customer}
                </button>
                {isCustomerOpen && (
                  <div className="nav-tree-apps">
                    {apps.map((app) => {
                      const active = activeCustomer === customer && activeApp === app;
                      return (
                        <Link
                          key={app}
                          to={`/?customer=${encodeURIComponent(customer)}&app=${encodeURIComponent(app)}`}
                          className={`nav-tree-app${active ? " active" : ""}`}
                        >
                          {app}
                        </Link>
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
