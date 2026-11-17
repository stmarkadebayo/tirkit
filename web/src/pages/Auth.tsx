import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import { Logo } from "../components/Layout";

function AuthCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="center">
      <div className="panel auth-card">
        <div className="brand" style={{ padding: "0 0 8px" }}>
          <span style={{ width: 22, height: 22, display: "inline-flex" }}>
            <Logo />
          </span>
          Tirkit
        </div>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const nav = useNavigate();
  const qc = useQueryClient();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await api("/api/auth/login", { method: "POST", json: { email, password } });
      await qc.invalidateQueries();
      nav("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in");
    }
  };
  return (
    <AuthCard title="Sign in">
      <form onSubmit={submit}>
        <input type="email" placeholder="Email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <input type="password" placeholder="Password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {error && <div className="error-box">{error}</div>}
        <button className="primary" type="submit">Sign in</button>
      </form>
    </AuthCard>
  );
}

export function Setup() {
  const [form, setForm] = useState({ email: "", password: "", name: "", project_name: "Default" });
  const [error, setError] = useState("");
  const nav = useNavigate();
  const qc = useQueryClient();
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const r = await api<{ project_id: string }>("/api/setup", { method: "POST", json: form });
      await qc.invalidateQueries();
      nav(`/p/${r.project_id}/settings?welcome=1`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Setup failed");
    }
  };
  return (
    <AuthCard title="Set up your Tirkit instance">
      <p className="muted small">Create the first admin account. It can invite others later.</p>
      <form onSubmit={submit}>
        <input placeholder="Your name" value={form.name} onChange={set("name")} />
        <input type="email" placeholder="Email" autoComplete="email" value={form.email} onChange={set("email")} required />
        <input type="password" placeholder="Password (10+ characters)" autoComplete="new-password" minLength={10} value={form.password} onChange={set("password")} required />
        <input placeholder="First project name" value={form.project_name} onChange={set("project_name")} />
        {error && <div className="error-box">{error}</div>}
        <button className="primary" type="submit">Create admin</button>
      </form>
    </AuthCard>
  );
}
