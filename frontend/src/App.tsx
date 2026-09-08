import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api, Application, Customer, DatabaseGroup, DbNode, errorMessage } from "./api";
import { AuthProvider, useAuth } from "./auth";
import ErrorBoundary from "./components/ErrorBoundary";
import { topologyOf } from "./formFields";
import AdminPage from "./pages/AdminPage";
import AlertsPage from "./pages/AlertsPage";
import CustomAlertRuleFormPage from "./pages/CustomAlertRuleFormPage";
import ApplicationsPage from "./pages/ApplicationsPage";
import CustomersPage from "./pages/CustomersPage";
import DashboardPage from "./pages/DashboardPage";
import DatabaseGroupsPage from "./pages/DatabaseGroupsPage";
import DatabaseWizardPage from "./pages/DatabaseWizardPage";
import ForcedPasswordChangePage from "./pages/ForcedPasswordChangePage";
import GroupDetailPage from "./pages/GroupDetailPage";
import InstanceDetailPage from "./pages/InstanceDetailPage";
import InstancesPage from "./pages/InstancesPage";
import LoginPage from "./pages/LoginPage";
import NotFoundPage from "./pages/NotFoundPage";
import PredictionsPage from "./pages/PredictionsPage";
import ReportsPage from "./pages/ReportsPage";
import ServersPage from "./pages/ServersPage";
import { ADD_ACTIONS } from "./terminology";

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
  // Aynı kural formFields.ts'teki `add_node` ile: standalone bir grup ikinci düğüm almaz.
  const isCluster = topologyOf(g.topology) === "cluster";
  return {
    id: `group-${g.id}`,
    name: isCluster && g.access_name ? g.access_name : g.name,
    href: `/groups/${g.id}`,
    meta: isCluster ? g.topology : undefined,
    loadChildren: () => api.getGroupNodes(g.id).then((nodes) => nodes.map(nodeLeaf)),
    // Standalone groups can't take a second node via the wizard (see wizard_add_nodes's 400) —
    // omit the "+" entry point entirely for them instead of linking to a dead end.
    emptyHref: isCluster ? `/groups/${g.id}/wizard` : undefined,
    emptyLabel: "+ Düğüm ekle",
  };
}

function applicationNode(a: Application): NavTreeNode {
  return {
    id: `app-${a.id}`,
    name: a.name,
    loadChildren: () => api.getGroups(a.id).then((groups) => groups.map(groupNode)),
    emptyHref: `/applications/${a.id}/groups/wizard`,
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
    // Clicking the name itself now goes straight to this customer's Uygulamalar page (İŞ 3) —
    // the separate toggle arrow still expands the tree to browse servers/apps/groups/nodes.
    href: `/customers/${c.id}/applications`,
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

function NavTreeBranch({ node, activePath, canWrite }: { node: NavTreeNode; activePath: string; canWrite: boolean }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<NavTreeNode[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
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
      <span className="nav-tree-instance" style={{ opacity: 0.5, cursor: "default" }} title="Bağlı veritabanı yok">
        {node.name}
      </span>
    );
  }

  const loadChildrenOnce = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setChildren(await node.loadChildren!());
    } catch (err) {
      // Eskiden burada catch yoktu: API düştüğünde dal sonsuza kadar "Yükleniyor…" kalıyor,
      // konsola yakalanmamış bir promise reddi düşüyordu. Artık hata görünür ve tekrarlanabilir.
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  const toggle = async () => {
    if (!open && children === null) await loadChildrenOnce();
    setOpen((v) => !v);
  };

  const retryChildren = async () => {
    setChildren(null);
    await loadChildrenOnce();
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
        {canWrite && node.emptyHref && (
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
          {!loading && loadError && (
            <button type="button" className="nav-tree-error" title={loadError} onClick={retryChildren}>
              Yüklenemedi — tekrar dene
            </button>
          )}
          {!loading && !loadError && children?.length === 0 && (
            canWrite && node.emptyHref ? (
              <Link to={node.emptyHref} className="nav-tree-instance">{node.emptyLabel ?? "Ekle"}</Link>
            ) : (
              <span className="muted-note" style={{ paddingLeft: "0.5rem" }}>Kayıt yok</span>
            )
          )}
          {children?.map((child) => (
            <NavTreeBranch key={child.id} node={child} activePath={activePath} canWrite={canWrite} />
          ))}
        </div>
      )}
    </div>
  );
}

function MainNavTree({
  isPrivate,
  privateCustomerId,
  canWrite,
}: {
  isPrivate: boolean;
  privateCustomerId: number | null;
  canWrite: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [roots, setRoots] = useState<NavTreeNode[] | null>(null);
  const [loadingRoots, setLoadingRoots] = useState(false);
  const [rootsError, setRootsError] = useState<string | null>(null);
  const location = useLocation();
  const activePath = location.pathname;

  useEffect(() => {
    setRoots(null);
    setIsOpen(false);
  }, [isPrivate, privateCustomerId]);

  const label = isPrivate ? "Uygulamalar" : "Müşteriler";
  // Private mode: root links straight to the one tenant's Uygulamalar list/manage page.
  // Public mode: root links straight to the Müşteriler list/add page.
  const rootHref = isPrivate ? (privateCustomerId != null ? `/customers/${privateCustomerId}/applications` : null) : "/customers";
  const isActiveRoot =
    activePath.startsWith("/customers") ||
    activePath.startsWith("/applications") ||
    activePath.startsWith("/groups") ||
    activePath.startsWith("/instances/");

  const loadRoots = async () => {
    if (!isOpen && roots === null) {
      setLoadingRoots(true);
      setRootsError(null);
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
      } catch (err) {
        setRootsError(errorMessage(err));
      } finally {
        setLoadingRoots(false);
      }
    }
    setIsOpen((v) => !v);
  };

  return (
    <div className="nav-group">
      <div className={`nav-link nav-tree-root${isActiveRoot ? " active" : ""}${isOpen ? " open" : ""}`}>
        {rootHref ? (
          <Link to={rootHref} className="nav-tree-root-label">{label}</Link>
        ) : (
          <span className="nav-tree-root-label">{label}</span>
        )}
        <button className="nav-tree-chevron-btn" onClick={loadRoots} title={isOpen ? "Daralt" : "Genişlet"}>
          <span className="nav-tree-chevron">{isOpen ? "▾" : "▸"}</span>
        </button>
      </div>
      {isOpen && (
        <div className="nav-tree">
          {loadingRoots && <span className="muted-note" style={{ paddingLeft: "0.5rem" }}>Yükleniyor…</span>}
          {!loadingRoots && rootsError && (
            <button
              type="button"
              className="nav-tree-error"
              title={rootsError}
              onClick={() => {
                setRoots(null);
                setIsOpen(false);
                void loadRoots();
              }}
            >
              Yüklenemedi — tekrar dene
            </button>
          )}
          {!loadingRoots && !rootsError && roots?.length === 0 && canWrite && (
            <Link
              to={isPrivate && privateCustomerId != null ? `/customers/${privateCustomerId}/applications` : "/customers"}
              className="nav-tree-instance"
            >
              {isPrivate ? ADD_ACTIONS.application : ADD_ACTIONS.customer}
            </Link>
          )}
          {roots?.map((node) => (
            <NavTreeBranch key={node.id} node={node} activePath={activePath} canWrite={canWrite} />
          ))}
        </div>
      )}
    </div>
  );
}

function AppShell() {
  const { user, logout } = useAuth();
  const canWrite = user?.role === "admin";
  const [isPrivate, setIsPrivate] = useState(false);
  const [privateCustomerId, setPrivateCustomerId] = useState<number | null>(null);
  // "Selected" customer for the always-visible "Uygulamalar" sidebar link (İŞ 3): in private
  // mode this is just the one tenant customer; in public mode it tracks whichever customer the
  // user last visited (derived from the URL), so the link stays meaningful without requiring
  // the tree to be expanded first.
  const [selectedCustomerId, setSelectedCustomerId] = useState<number | null>(null);
  const location = useLocation();

  useEffect(() => {
    api.getConfig().then((cfg) => {
      setIsPrivate(cfg.deployment_mode === "private");
      if (cfg.deployment_mode === "private") {
        api.getCustomers().then((all: Customer[]) => {
          if (all.length > 0) {
            setPrivateCustomerId(all[0].id);
            setSelectedCustomerId(all[0].id);
          }
        }).catch(() => undefined);
      }
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (isPrivate) return;
    const match = location.pathname.match(/^\/customers\/(\d+)/);
    if (match) setSelectedCustomerId(Number(match[1]));
  }, [location.pathname, isPrivate]);

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
          <MainNavTree isPrivate={isPrivate} privateCustomerId={privateCustomerId} canWrite={canWrite} />
          {/* Private mode's tree root already links straight to the one tenant's Uygulamalar
              page (see MainNavTree) — this fixed link only adds value in public mode, where the
              root links to Müşteriler instead and "which customer" varies by what's selected. */}
          {!isPrivate && (
            <NavLink
              to={selectedCustomerId != null ? `/customers/${selectedCustomerId}/applications` : "/customers"}
              className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
            >
              Uygulamalar
            </NavLink>
          )}
          {isPrivate && privateCustomerId != null && (
            <NavLink
              to={`/customers/${privateCustomerId}/servers`}
              className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
            >
              Sunucular
            </NavLink>
          )}
          <NavLink to="/instances" end className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Veritabanları
          </NavLink>
          <NavLink to="/reports" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Raporlar
          </NavLink>
          <NavLink to="/predictions" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Predictions
          </NavLink>
          <NavLink to="/alerts" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Alerts
          </NavLink>
          {canWrite && (
            <NavLink to="/admin" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
              Yönetim
            </NavLink>
          )}
        </nav>
        <div className="sidebar-user">
          <div className="sidebar-user-info">
            <strong>{user?.username}</strong>
            <span className={`tag ${user?.role === "admin" ? "public" : "private"}`}>{user?.role}</span>
          </div>
          <button type="button" className="btn btn-xs" onClick={logout}>
            Çıkış yap
          </button>
        </div>
      </aside>
      <main className="main">
        {/* Hata sınırı rota BAŞINA sıfırlanıyor (resetKey): bir sayfa patladıktan sonra
            kenar çubuğundan başka bir sayfaya geçildiğinde eski hata ekranda kalmasın. */}
        <ErrorBoundary resetKey={location.pathname}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/instances" element={<InstancesPage />} />
          <Route path="/instances/:id" element={<InstanceDetailPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/predictions" element={<PredictionsPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/alerts/new" element={<CustomAlertRuleFormPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/customers" element={<CustomersPage />} />
          <Route path="/customers/:customerId/applications" element={<ApplicationsPage />} />
          <Route path="/customers/:customerId/servers" element={<ServersPage />} />
          <Route path="/applications/:applicationId/groups" element={<DatabaseGroupsPage />} />
          <Route path="/applications/:applicationId/groups/wizard" element={<DatabaseWizardPage />} />
          <Route path="/groups/:groupId" element={<GroupDetailPage />} />
          <Route path="/groups/:groupId/wizard" element={<DatabaseWizardPage />} />
          {/* Catch-all: eşleşmeyen adres eskiden BOŞ bir içerik alanı render ediyordu. */}
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}

function AuthGate() {
  const { user, loading } = useAuth();
  if (loading) return <div className="auth-loading">Yükleniyor…</div>;
  if (!user) return <LoginPage />;
  if (user.must_change_password) return <ForcedPasswordChangePage />;
  return <AppShell />;
}

export default function App() {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  );
}
