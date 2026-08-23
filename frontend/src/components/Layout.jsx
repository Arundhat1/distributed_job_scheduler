import { NavLink, useNavigate } from "react-router-dom";

const NAV = [
  { to: "/", label: "Overview", icon: "◧" },
  { to: "/queues", label: "Queues", icon: "▤" },
  { to: "/workers", label: "Workers", icon: "◈" },
];

export default function Layout({ children, title, subtitle, connected }) {
  const navigate = useNavigate();

  function logout() {
    localStorage.removeItem("token");
    navigate("/login");
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="dot" />
          scheduler
        </div>
        {NAV.map((item) => (
          <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}>
            <span>{item.icon}</span> {item.label}
          </NavLink>
        ))}
        <div style={{ marginTop: "auto", paddingTop: 20 }}>
          <button className="btn btn-sm" onClick={logout} style={{ width: "100%" }}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="main">
        <div className="topbar">
          <div>
            <h1 className="page-title">{title}</h1>
            {subtitle && <div className="page-subtitle">{subtitle}</div>}
          </div>
          <div className="ws-indicator">
            <span className={"pulse-dot" + (connected ? " live" : "")} />
            {connected ? "live" : "reconnecting…"}
          </div>
        </div>
        {children}
      </main>
    </div>
  );
}