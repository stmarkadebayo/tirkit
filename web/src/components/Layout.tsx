import { Suspense, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useMe, usePid } from "../hooks";
import { NAV } from "../routes";

export function Logo() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden>
      <path d="M16 3c-5 6-7 12-6 18 1 4 3 7 6 8 3-1 5-4 6-8 1-6-1-12-6-18z" fill="var(--accent)" />
      <path d="M16 6v22" stroke="var(--panel)" strokeWidth="1.5" />
    </svg>
  );
}

const SECTIONS = ["", "Observe", "Evaluate", "Build"] as const;

export default function Layout() {
  const pid = usePid();
  const me = useMe();
  const nav = useNavigate();
  const qc = useQueryClient();
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);
  const menuBtn = useRef<HTMLButtonElement>(null);
  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    qc.clear();
    nav("/login");
  };

  // The drawer closes when you go somewhere, and on Escape (returning focus to the menu button).
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        menuBtn.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const links = (items: typeof NAV) =>
    items.map((i) => (
      <NavLink key={i.to} to={`/p/${pid}/${i.to}`}>
        {i.label}
      </NavLink>
    ));
  const top = NAV.filter((i) => i.section === "" && i.to === "overview");
  const bottom = NAV.filter((i) => i.section === "" && i.to !== "overview");

  return (
    <div className={`app${open ? " nav-open" : ""}`}>
      <header className="topbar">
        <button
          ref={menuBtn}
          className="icon-btn"
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          aria-controls="sidebar"
          onClick={() => setOpen(!open)}
        >
          <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden>
            <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
        <div className="brand">
          <Logo /> Tirkit
        </div>
      </header>
      <div className="scrim" onClick={() => setOpen(false)} aria-hidden />
      <aside className="sidebar" id="sidebar" aria-label="Main navigation">
        <div className="brand">
          <Logo /> Tirkit
          <button className="icon-btn drawer-close" aria-label="Close menu" onClick={() => { setOpen(false); menuBtn.current?.focus(); }}>
            <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden>
              <path d="M4 4l12 12M16 4L4 16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        <select aria-label="Project" value={pid} onChange={(e) => nav(`/p/${e.target.value}/overview`)} style={{ margin: "0 4px 8px" }}>
          {me.data?.projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>
              {p.name}
            </option>
          ))}
        </select>
        <nav aria-label="Pages">
          {SECTIONS.map((sec) => {
            if (sec === "") return <div key="top" className="nav">{links(top)}</div>;
            const secItems = NAV.filter((i) => i.section === sec);
            return (
              <div key={sec} className="nav">
                <div className="nav-section">{sec}</div>
                {links(secItems)}
              </div>
            );
          })}
          <div className="nav nav-last">{links(bottom)}</div>
        </nav>
        <div className="sidebar-foot">
          <div className="muted truncate-line">{me.data?.user.email}</div>
          <a href="#" onClick={(e) => { e.preventDefault(); void logout(); }}>
            Sign out
          </a>
        </div>
      </aside>
      <main className="main">
        <Suspense fallback={<div className="muted" role="status">Loading…</div>}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
