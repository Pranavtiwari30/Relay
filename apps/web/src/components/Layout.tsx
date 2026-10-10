import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/integrations", label: "Integrations" },
  { to: "/events", label: "Events" },
  { to: "/failures", label: "Failures" },
  { to: "/investigations", label: "Investigations" },
  { to: "/audit", label: "Audit log" },
  { to: "/settings", label: "Settings" },
];

export function Layout() {
  const { session, workspace, logout, switchWorkspace } = useAuth();
  if (!session) return null;
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <NavLink to="/" className="brand" aria-label="Relay home">
          <span className="brand-mark" aria-hidden="true">
            ⇥
          </span>
          Relay
        </NavLink>
        <nav className="nav" aria-label="Primary">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          {session.workspaces.length > 1 ? (
            <label className="field">
              <span className="field-label">Workspace</span>
              <select
                value={session.active_workspace_id ?? ""}
                onChange={(e) => void switchWorkspace(e.target.value)}
              >
                {session.workspaces.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div>
              <div className="field-label">Workspace</div>
              <div>{workspace?.name ?? "None"}</div>
            </div>
          )}
          <div>
            <div className="truncate" title={session.user.email}>
              {session.user.email}
            </div>
            <div className="muted">Role: {session.role ?? "none"}</div>
          </div>
          <button type="button" className="btn btn-sm" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="main" id="main" tabIndex={-1}>
        <div className="content">
          {workspace?.is_demo && (
            <div className="demo-banner" role="note">
              <strong>Demo workspace.</strong>
              <span>
                Events here come from the built-in simulator and are delivered to a built-in demo destination.
                Figures are computed from those events; none are illustrative.
              </span>
            </div>
          )}
          <Outlet />
        </div>
      </main>
    </div>
  );
}
