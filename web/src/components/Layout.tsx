import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useMe, usePid } from "../hooks";
import { extraNav, type NavItem } from "../routes";

const BASE_NAV: NavItem[] = [
  { to: "dashboard", label: "Dashboard", section: "Observe" },
  { to: "traces", label: "Traces", section: "Observe" },
  { to: "sessions", label: "Sessions", section: "Observe" },
  { to: "users", label: "Users", section: "Observe" },
];

export function Logo() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden>
      <path d="M16 3c-5 6-7 12-6 18 1 4 3 7 6 8 3-1 5-4 6-8 1-6-1-12-6-18z" fill="var(--accent)" />
      <path d="M16 6v22" stroke="var(--panel)" strokeWidth="1.5" />
    </svg>
  );
}

export default function Layout() {
  const pid = usePid();
  const me = useMe();
  const nav = useNavigate();
  const qc = useQueryClient();
  const items = [...BASE_NAV, ...extraNav, { to: "settings", label: "Settings", section: "Project" } as NavItem];
  const sections = ["Observe", "Evaluate", "Iterate", "Project"] as const;
  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    qc.clear();
    nav("/login");
  };
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <Logo /> Tirkit
        </div>
        <select
          aria-label="Project"
          value={pid}
          onChange={(e) => nav(`/p/${e.target.value}/dashboard`)}
          style={{ margin: "0 4px 8px" }}
        >
          {me.data?.projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>
              {p.name}
            </option>
          ))}
        </select>
        {sections.map((sec) => {
          const secItems = items.filter((i) => i.section === sec);
          if (!secItems.length) return null;
          return (
            <div key={sec} className="nav">
              <div className="nav-section">{sec}</div>
              {secItems.map((i) => (
                <NavLink key={i.to} to={`/p/${pid}/${i.to}`}>
                  {i.label}
                </NavLink>
              ))}
            </div>
          );
        })}
        <div className="sidebar-foot">
          <div className="muted">{me.data?.user.email}</div>
          <a href="#" onClick={(e) => { e.preventDefault(); void logout(); }}>
            Sign out
          </a>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
