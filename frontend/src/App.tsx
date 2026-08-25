import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import { api, Application, Customer, DatabaseGroup, InstanceSummary } from "./api";
import AlertsPage from "./pages/AlertsPage";
import ApplicationsPage from "./pages/ApplicationsPage";
import CustomersPage from "./pages/CustomersPage";
import DashboardPage from "./pages/DashboardPage";
import DatabaseGroupsPage from "./pages/DatabaseGroupsPage";
import GroupDetailPage from "./pages/GroupDetailPage";
import InstanceDetailPage from "./pages/InstanceDetailPage";
import InstancesPage from "./pages/InstancesPage";
import PredictionsPage from "./pages/PredictionsPage";

// --- Legacy Instance-based nav (Instance.customer_name/application/cluster_name strings).
// Kept as a standalone bottom link, separate from the real Customer/Application/DatabaseGroup
// tree below — the two used to sit side by side and both looked like a "customer" entry.
type LegacyAppNode = {
  name: string;
  instances: { id: number; name: string; cluster_name: string | null }[];
};

type LegacyCustomerNode = {
  name: string;
  apps: LegacyAppNode[];
};

function LegacyInstanceTree() {
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

  const tree = useMemo<LegacyCustomerNode[]>(() => {
    const customerMap = new Map<string, Map<string, LegacyAppNode>>();
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
    const result: LegacyCustomerNode[] = [];
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
        <span>Instance Gezgini</span>
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
                                <Link
                                  key={inst.id}
                                  to={`/instances/${inst.id}?tab=tuning`}
                                  className="nav-tree-instance"
                                  title={`${inst.name} · Tuning`}
                                >
                                  {inst.name}
                                  {inst.cluster_name && <span className="nav-tree-cluster">{inst.cluster_name}</span>}
                                </Link>
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

// --- Real navigation tree: Customer → Application → DatabaseGroup (public mode) or
// Application → DatabaseGroup (private mode, customer level skipped since there's only one).
// Only the leaf (a group) navigates on click; customer/application rows just expand/collapse.
type NavTreeNode = {
  id: string;
  name: string;
  href?: string;
  meta?: string;
  loadChildren?: () => Promise<NavTreeNode[]>;
  // Shown instead of "Kayıt yok" when loadChildren resolves empty, so an empty branch
  // doesn't become a dead end for reaching the create form.
  emptyHref?: string;
  emptyLabel?: string;
};

function groupNode(g: DatabaseGroup): NavTreeNode {
  const isCluster = g.topology !== "standalone";
  return {
    id: `group-${g.id}`,
    name: isCluster && g.access_name ? g.access_name : g.name,
    href: `/groups/${g.id}`,
    meta: isCluster ? g.topology : undefined,
  };
}

function applicationNode(a: Application): NavTreeNode {
  return {
    id: `app-${a.id}`,
    name: a.name,
    loadChildren: () => api.getGroups(a.id).then((groups) => groups.map(groupNode)),
    emptyHref: `/applications/${a.id}/groups`,
    emptyLabel: "+ Grup ekle",
  };
}

function customerNode(c: Customer): NavTreeNode {
  return {
    id: `customer-${c.id}`,
    name: c.name,
    loadChildren: () => api.getApplications(c.id).then((apps) => apps.map(applicationNode)),
    emptyHref: `/customers/${c.id}/applications`,
    emptyLabel: "+ Uygulama ekle",
  };
}

function NavTreeBranch({ node, activePath }: { node: NavTreeNode; activePath: string }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<NavTreeNode[] | null>(null);
  const [loading, setLoading] = useState(false);

  if (node.href) {
    const isActive = activePath === node.href;
    return (
      <Link to={node.href} className={`nav-tree-instance${isActive ? " active" : ""}`}>
        {node.name}
        {node.meta && <span className="nav-tree-cluster">{node.meta}</span>}
      </Link>
    );
  }

  const toggle = async () => {
    if (!open && children === null && node.loadChildren) {
      setLoading(true);
      try {
        setChildren(await node.loadChildren());
      } finally {
        setLoading(false);
      }
    }
    setOpen((v) => !v);
  };

  return (
    <div className="nav-tree-section">
      <button className="nav-tree-customer" onClick={toggle}>
        <span className={`nav-tree-arrow${open ? " open" : ""}`}>▶</span>
        {node.name}
      </button>
      {open && (
        <div className="nav-tree-apps">
          {loading && <span className="muted-note" style={{ paddingLeft: "0.5rem" }}>Yükleniyor…</span>}
          {!loading && children?.length === 0 && (
            node.emptyHref ? (
              <Link to={node.emptyHref} className="nav-tree-instance">{node.emptyLabel ?? "Ekle"}</Link>
            ) : (
              <span className="muted-note" style={{ paddingLeft: "0.5rem" }}>Kayıt yok</span>
            )
          )}
          {children?.map((child) => (
            <NavTreeBranch key={child.id} node={child} activePath={activePath} />
          ))}
        </div>
      )}
    </div>
  );
}

function MainNavTree({ isPrivate, privateCustomerId }: { isPrivate: boolean; privateCustomerId: number | null }) {
  const [isOpen, setIsOpen] = useState(false);
  const [roots, setRoots] = useState<NavTreeNode[] | null>(null);
  const [loadingRoots, setLoadingRoots] = useState(false);
  const location = useLocation();
  const activePath = location.pathname;

  useEffect(() => {
    setRoots(null);
    setIsOpen(false);
  }, [isPrivate, privateCustomerId]);

  const label = isPrivate ? "Uygulamalar" : "Müşteriler";
  const isActiveRoot =
    activePath.startsWith("/customers") || activePath.startsWith("/applications") || activePath.startsWith("/groups");

  const toggleRoot = async () => {
    if (!isOpen && roots === null) {
      setLoadingRoots(true);
      try {
        if (isPrivate) {
          if (privateCustomerId == null) {
            setRoots([]);
          } else {
            const apps = await api.getApplications(privateCustomerId);
            setRoots(apps.map(applicationNode));
          }
        } else {
          const customers = await api.getCustomers();
          setRoots(customers.map(customerNode));
        }
      } finally {
        setLoadingRoots(false);
      }
    }
    setIsOpen((v) => !v);
  };

  return (
    <div className="nav-group">
      <button
        className={`nav-link nav-tree-root${isActiveRoot ? " active" : ""}${isOpen ? " open" : ""}`}
        onClick={toggleRoot}
      >
        <span>{label}</span>
        <span className="nav-tree-chevron">{isOpen ? "▾" : "▸"}</span>
      </button>
      {isOpen && (
        <div className="nav-tree">
          {loadingRoots && <span className="muted-note" style={{ paddingLeft: "0.5rem" }}>Yükleniyor…</span>}
          {!loadingRoots && roots?.length === 0 && (
            <Link
              to={isPrivate && privateCustomerId != null ? `/customers/${privateCustomerId}/applications` : "/customers"}
              className="nav-tree-instance"
            >
              {isPrivate ? "+ Uygulama ekle" : "+ Müşteri ekle"}
            </Link>
          )}
          {roots?.map((node) => (
            <NavTreeBranch key={node.id} node={node} activePath={activePath} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [isPrivate, setIsPrivate] = useState(false);
  const [privateCustomerId, setPrivateCustomerId] = useState<number | null>(null);

  useEffect(() => {
    api.getConfig().then((cfg) => {
      setIsPrivate(cfg.deployment_mode === "private");
      if (cfg.deployment_mode === "private") {
        api.getCustomers().then((all: Customer[]) => {
          if (all.length > 0) setPrivateCustomerId(all[0].id);
        }).catch(() => undefined);
      }
    }).catch(() => undefined);
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">DB</div>
          <div>
            <h1>dbace</h1>
            <p>DBA monitoring platform</p>
          </div>
        </div>
        <nav>
          <NavLink to="/" end className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Dashboard
          </NavLink>
          <MainNavTree isPrivate={isPrivate} privateCustomerId={privateCustomerId} />
          <NavLink to="/instances" end className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Instances
          </NavLink>
          <NavLink to="/predictions" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Predictions
          </NavLink>
          <NavLink to="/alerts" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Alerts
          </NavLink>
          <LegacyInstanceTree />
        </nav>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/instances" element={<InstancesPage />} />
          <Route path="/instances/:id" element={<InstanceDetailPage />} />
          <Route path="/predictions" element={<PredictionsPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/customers" element={<CustomersPage />} />
          <Route path="/customers/:customerId/applications" element={<ApplicationsPage />} />
          <Route path="/applications/:applicationId/groups" element={<DatabaseGroupsPage />} />
          <Route path="/groups/:groupId" element={<GroupDetailPage />} />
        </Routes>
      </main>
    </div>
  );
}
