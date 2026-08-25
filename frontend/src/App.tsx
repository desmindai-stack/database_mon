import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api, Application, Customer, DatabaseGroup, DbNode } from "./api";
import AlertsPage from "./pages/AlertsPage";
import ApplicationsPage from "./pages/ApplicationsPage";
import CustomersPage from "./pages/CustomersPage";
import DashboardPage from "./pages/DashboardPage";
import DatabaseGroupsPage from "./pages/DatabaseGroupsPage";
import GroupDetailPage from "./pages/GroupDetailPage";
import InstanceDetailPage from "./pages/InstanceDetailPage";
import InstancesPage from "./pages/InstancesPage";
import PredictionsPage from "./pages/PredictionsPage";
import ServersPage from "./pages/ServersPage";

// Real navigation tree: Customer → Application → DatabaseGroup → Node (public mode) or
// Application → DatabaseGroup → Node (private mode, customer level skipped since there's
// only one). Only leaves that have somewhere to go navigate on click — a group links to
// Group Detail while also expanding to show its nodes; a node links to its linked Instance's
// detail page (metrics/slow queries/index advice/explain) when one is linked, otherwise it's
// shown as plain text (nothing to visit yet — no credentials configured for that node).
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

function nodeLeaf(n: DbNode): NavTreeNode {
  const meta = [n.instance_name, n.role_hint !== "unknown" ? n.role_hint : null].filter(Boolean).join(" · ");
  return {
    id: `node-${n.id}`,
    name: n.name,
    href: n.instance_id != null ? `/instances/${n.instance_id}` : undefined,
    meta: meta || undefined,
  };
}

function groupNode(g: DatabaseGroup): NavTreeNode {
  const isCluster = g.topology !== "standalone";
  return {
    id: `group-${g.id}`,
    name: isCluster && g.access_name ? g.access_name : g.name,
    href: `/groups/${g.id}`,
    meta: isCluster ? g.topology : undefined,
    loadChildren: () => api.getGroupNodes(g.id).then((nodes) => nodes.map(nodeLeaf)),
    emptyHref: `/groups/${g.id}`,
    emptyLabel: "+ Düğüm ekle",
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

function serversNode(customerId: number): NavTreeNode {
  return { id: `servers-${customerId}`, name: "📁 Sunucular", href: `/customers/${customerId}/servers` };
}

function customerNode(c: Customer): NavTreeNode {
  return {
    id: `customer-${c.id}`,
    name: c.name,
    // Always has at least the "Sunucular" entry, so the generic empty-branch fallback
    // (emptyHref/emptyLabel) never kicks in here — add the "+ Uygulama ekle" leaf explicitly
    // when there are no applications yet, instead.
    loadChildren: () =>
      api.getApplications(c.id).then((apps) => [
        serversNode(c.id),
        ...(apps.length === 0
          ? [{ id: `customer-${c.id}-add-app`, name: "+ Uygulama ekle", href: `/customers/${c.id}/applications` }]
          : apps.map(applicationNode)),
      ]),
  };
}

function NavTreeBranch({ node, activePath }: { node: NavTreeNode; activePath: string }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<NavTreeNode[] | null>(null);
  const [loading, setLoading] = useState(false);
  const isActive = node.href != null && activePath === node.href;

  // Pure leaf — nothing beneath it (a node, or a group/app/customer that never gets children).
  if (!node.loadChildren) {
    if (node.href) {
      return (
        <Link to={node.href} className={`nav-tree-instance${isActive ? " active" : ""}`}>
          {node.name}
          {node.meta && <span className="nav-tree-cluster">{node.meta}</span>}
        </Link>
      );
    }
    return (
      <span className="nav-tree-instance" style={{ opacity: 0.5, cursor: "default" }} title="Bağlı instance yok">
        {node.name}
      </span>
    );
  }

  const toggle = async () => {
    if (!open && children === null) {
      setLoading(true);
      try {
        setChildren(await node.loadChildren!());
      } finally {
        setLoading(false);
      }
    }
    setOpen((v) => !v);
  };

  // Branch — expandable, and optionally also a link (e.g. a group links to Group Detail
  // while the separate toggle arrow expands to show its nodes).
  return (
    <div className="nav-tree-section">
      <div className="nav-tree-app-row">
        {node.href ? (
          <Link to={node.href} className={`nav-tree-app${isActive ? " active" : ""}`}>
            {node.name}
          </Link>
        ) : (
          <button className="nav-tree-app" style={{ textAlign: "left" }} onClick={toggle}>
            {node.name}
          </button>
        )}
        {node.emptyHref && (
          <Link to={node.emptyHref} className="nav-tree-add-btn" title={node.emptyLabel ?? "Ekle"}>
            +
          </Link>
        )}
        <button className={`nav-tree-app-toggle${open ? " open" : ""}`} onClick={toggle}>
          ▶
        </button>
      </div>
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
    activePath.startsWith("/customers") ||
    activePath.startsWith("/applications") ||
    activePath.startsWith("/groups") ||
    activePath.startsWith("/instances/");

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
          {isPrivate && privateCustomerId != null && (
            <NavLink
              to={`/customers/${privateCustomerId}/servers`}
              className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
            >
              Sunucular
            </NavLink>
          )}
          <NavLink to="/instances" end className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
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
          <Route path="/customers" element={<CustomersPage />} />
          <Route path="/customers/:customerId/applications" element={<ApplicationsPage />} />
          <Route path="/customers/:customerId/servers" element={<ServersPage />} />
          <Route path="/applications/:applicationId/groups" element={<DatabaseGroupsPage />} />
          <Route path="/groups/:groupId" element={<GroupDetailPage />} />
        </Routes>
      </main>
    </div>
  );
}
