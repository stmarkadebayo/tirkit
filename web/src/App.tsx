import { lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { useMe } from "./hooks";
import Layout from "./components/Layout";
import { Login, Setup } from "./pages/Auth";
import Overview from "./pages/Overview";
import { extraRoutes } from "./routes";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Traces = lazy(() => import("./pages/Traces"));
const TraceView = lazy(() => import("./pages/TraceView"));
const Sessions = lazy(() => import("./pages/Sessions").then((m) => ({ default: m.Sessions })));
const SessionView = lazy(() => import("./pages/Sessions").then((m) => ({ default: m.SessionView })));
const Users = lazy(() => import("./pages/Users"));
const Settings = lazy(() => import("./pages/Settings"));

function Home() {
  const setup = useQuery({ queryKey: ["setup"], queryFn: () => api<{ needs_setup: boolean }>("/api/setup") });
  const me = useMe();
  if (setup.data?.needs_setup) return <Navigate to="/setup" replace />;
  if (me.isLoading || setup.isLoading) return <div className="center muted">Loading…</div>;
  if (!me.data) return <Navigate to="/login" replace />;
  const first = me.data.projects[0];
  if (!first) return <div className="center">You are not a member of any project yet. Ask an admin to add you.</div>;
  return <Navigate to={`/p/${first.project_id}/overview`} replace />;
}

function RequireAuth({ children }: { children: JSX.Element }) {
  const me = useMe();
  if (me.isLoading) return <div className="center muted">Loading…</div>;
  if (!me.data) return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/login" element={<Login />} />
      <Route path="/setup" element={<Setup />} />
      <Route
        path="/p/:pid"
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="overview" replace />} />
        <Route path="overview" element={<Overview />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="traces" element={<Traces />} />
        <Route path="traces/:tid" element={<TraceView />} />
        <Route path="sessions" element={<Sessions />} />
        <Route path="sessions/:sid" element={<SessionView />} />
        <Route path="users" element={<Users />} />
        <Route path="settings" element={<Settings />} />
        {extraRoutes}
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
