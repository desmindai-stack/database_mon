import { NavLink, Route, Routes } from "react-router-dom";
import AlertsPage from "./pages/AlertsPage";
import DashboardPage from "./pages/DashboardPage";
import InstanceDetailPage from "./pages/InstanceDetailPage";
import InstancesPage from "./pages/InstancesPage";

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">PG</div>
          <div>
            <h1>pgwatch</h1>
            <p>PostgreSQL monitoring</p>
          </div>
        </div>
        <nav>
          <NavLink to="/" end className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Dashboard
          </NavLink>
          <NavLink to="/instances" className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            Instances
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
          <Route path="/alerts" element={<AlertsPage />} />
        </Routes>
      </main>
    </div>
  );
}
