import { useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useTheme } from "../theme/ThemeContext";
import type { ThemePreference } from "../theme/ThemeContext";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

export function RequireAuth() {
  const { claims } = useAuth();
  const location = useLocation();
  if (claims === null) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

export function RequireRole({ roles }: { roles: string[] }) {
  const { hasRole } = useAuth();
  if (!hasRole(...roles)) {
    return (
      <section className="card narrow-card">
        <h1>Not available</h1>
        <p>Your account does not have access to this page.</p>
      </section>
    );
  }
  return <Outlet />;
}

const NAV_KEY = "ekc.nav.collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(NAV_KEY) === "1";
  } catch {
    return false;
  }
}

const THEMES: { value: ThemePreference; label: string; icon: IconName }[] = [
  { value: "system", label: "System theme", icon: "monitor" },
  { value: "light", label: "Light theme", icon: "sun" },
  { value: "dark", label: "Dark theme", icon: "moon" },
];

/** Navigation visibility is presentation only: the API enforces every role check itself. */
export function Layout() {
  const { claims, signOut, hasRole } = useAuth();
  const { preference, setPreference } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [open, setOpen] = useState(false);
  const main = useRef<HTMLElement>(null);
  const first = useRef(true);

  // Move focus to the page on navigation (screen readers/keyboard users), and close the mobile drawer.
  useEffect(() => {
    setOpen(false);
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus();
  }, [location.pathname]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      try {
        localStorage.setItem(NAV_KEY, c ? "0" : "1");
      } catch {
        // per-viewer convenience only
      }
      return !c;
    });
  };

  // An explicit sign-out must not leave a return path behind: the next person to sign in starts at "/".
  const onSignOut = () => {
    navigate("/login", { replace: true, state: { from: "/" } });
    signOut();
  };

  const link = (to: string, label: string, icon: IconName, end = false) => (
    <NavLink to={to} end={end} title={label}>
      <Icon name={icon} />
      <span className="nav-label">{label}</span>
    </NavLink>
  );

  return (
    <div className={`shell${collapsed ? " collapsed" : ""}${open ? " nav-open" : ""}`}>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <button
          type="button"
          className="icon-btn"
          aria-label="Open navigation menu"
          aria-expanded={open}
          aria-controls="sidebar"
          onClick={() => {
            setOpen(true);
          }}
        >
          <Icon name="menu" />
        </button>
        <strong className="brand-name">Knowledge Copilot</strong>
      </header>
      <aside id="sidebar" className="sidebar">
        <div className="sidebar-head">
          <span className="brand-mark" aria-hidden="true">
            <Icon name="ask" size={18} />
          </span>
          <strong className="brand-name collapsible">Knowledge Copilot</strong>
          <button
            type="button"
            className="icon-btn mobile-only"
            aria-label="Close navigation menu"
            onClick={() => {
              setOpen(false);
            }}
          >
            <Icon name="close" />
          </button>
        </div>
        <nav aria-label="Main">
          {link("/", "Ask", "ask", true)}
          {link("/documents", "Documents", "documents")}
          {hasRole("admin") && link("/usage", "Usage", "usage")}
          {hasRole("admin", "security_auditor") && link("/audit", "Audit", "audit")}
          {link("/system", "System status", "system")}
        </nav>
        <div className="sidebar-foot">
          <div className="who">
            <span className="avatar" aria-hidden="true">
              {(claims?.email ?? "?").slice(0, 1).toUpperCase()}
            </span>
            <span className="collapsible who-text">
              <span className="who-email">{claims?.email}</span>
              <span className="muted who-roles">{claims !== null && claims.roles.length > 0 ? claims.roles.join(", ") : "no roles"}</span>
            </span>
          </div>
          <div className="theme-switch collapsible" role="group" aria-label="Colour theme">
            {THEMES.map((t) => (
              <button
                key={t.value}
                type="button"
                className="icon-btn"
                aria-label={t.label}
                aria-pressed={preference === t.value}
                onClick={() => {
                  setPreference(t.value);
                }}
              >
                <Icon name={t.icon} size={16} />
              </button>
            ))}
          </div>
          <button type="button" className="secondary signout" onClick={onSignOut}>
            <Icon name="logout" />
            <span className="nav-label">Sign out</span>
          </button>
          <button
            type="button"
            className="icon-btn collapse-btn desktop-only"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-pressed={collapsed}
            onClick={toggleCollapsed}
          >
            <Icon name={collapsed ? "chevronRight" : "chevronLeft"} />
          </button>
        </div>
      </aside>
      <div
        className="nav-scrim"
        aria-hidden="true"
        onClick={() => {
          setOpen(false);
        }}
      />
      <main id="main" ref={main} tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
