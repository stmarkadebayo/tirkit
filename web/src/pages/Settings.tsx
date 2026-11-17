import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type ApiKeyInfo as Key, type Member, type Project } from "../api";
import { usePid, useRole } from "../hooks";
import { fmtAgo } from "../format";

export default function Settings() {
  const pid = usePid();
  const role = useRole();
  const [params] = useSearchParams();
  const isAdmin = role === "admin";
  return (
    <div style={{ maxWidth: 980 }}>
      <div className="page-head">
        <div>
          <h1>Project settings</h1>
          <div className="muted small mono">{pid}</div>
        </div>
      </div>
      {params.get("welcome") && (
        <div className="notice" style={{ marginBottom: 14 }}>
          Welcome. Create an API key below, then send your first trace with the SDK or any OpenTelemetry exporter.
        </div>
      )}
      <div className="grid">
        {isAdmin && <Keys pid={pid} />}
        <Members pid={pid} isAdmin={isAdmin} />
        {isAdmin && <DataSettings pid={pid} />}
        <Prices pid={pid} isAdmin={isAdmin} />
      </div>
    </div>
  );
}

function Keys({ pid }: { pid: string }) {
  const qc = useQueryClient();
  const keys = useQuery({ queryKey: ["keys", pid], queryFn: () => api<Key[]>(projectPath(pid, "/keys")) });
  const [created, setCreated] = useState<{ public_key: string; secret_key: string } | null>(null);
  const [name, setName] = useState("");
  const host = window.location.origin;
  const create = async () => {
    const r = await api<{ public_key: string; secret_key: string }>(projectPath(pid, "/keys"), { method: "POST", json: { name: name || null } });
    setCreated(r);
    setName("");
    void qc.invalidateQueries({ queryKey: ["keys", pid] });
  };
  const revoke = async (pk: string) => {
    if (!confirm(`Revoke ${pk}? Apps using it will stop sending traces.`)) return;
    await api(projectPath(pid, `/keys/${pk}`), { method: "DELETE" });
    void qc.invalidateQueries({ queryKey: ["keys", pid] });
  };
  return (
    <div className="panel">
      <h2>API keys</h2>
      <p className="muted small">Keys let SDKs, OpenTelemetry exporters and CI send traces and scores to this project only.</p>
      {created && (
        <div className="grid" style={{ marginBottom: 12 }}>
          <div className="notice">Copy the secret now. It is stored hashed and cannot be shown again.</div>
          <div className="secret">TIRKIT_HOST={host}
            <br />TIRKIT_PUBLIC_KEY={created.public_key}
            <br />TIRKIT_SECRET_KEY={created.secret_key}</div>
          <details open>
            <summary>Python SDK</summary>
            <pre className="json">{`pip install tirkit
import tirkit
tirkit.init()            # reads TIRKIT_HOST / TIRKIT_PUBLIC_KEY / TIRKIT_SECRET_KEY
tirkit.instrument()      # auto-traces OpenAI and Anthropic calls

with tirkit.span("my-step", kind="llm", model="gpt-4o", input=prompt) as s:
    s.output = call_model(prompt)`}</pre>
          </details>
          <details>
            <summary>OpenTelemetry (any language)</summary>
            <pre className="json">{`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=${host}/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS=Authorization=Basic%20${btoa(`${created.public_key}:${created.secret_key}`)}`}</pre>
          </details>
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>Public key</th>
            <th>Name</th>
            <th>Created</th>
            <th>Last used</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {keys.data?.map((k) => (
            <tr key={k.public_key}>
              <td className="mono">{k.public_key}</td>
              <td>{k.name}</td>
              <td className="small muted">{fmtAgo(k.created)}</td>
              <td className="small muted">{k.last_used ? fmtAgo(k.last_used) : "never"}</td>
              <td>
                <button className="danger" onClick={() => revoke(k.public_key)}>Revoke</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 10 }}>
        <input placeholder="Key name (e.g. production)" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="primary" onClick={create}>Create API key</button>
      </div>
    </div>
  );
}

function Members({ pid, isAdmin }: { pid: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const members = useQuery({ queryKey: ["members", pid], queryFn: () => api<Member[]>(projectPath(pid, "/members")) });
  const [form, setForm] = useState({ email: "", role: "member", password: "" });
  const [err, setErr] = useState("");
  const add = async () => {
    setErr("");
    try {
      await api(projectPath(pid, "/members"), { method: "POST", json: { ...form, password: form.password || undefined } });
      setForm({ email: "", role: "member", password: "" });
      void qc.invalidateQueries({ queryKey: ["members", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  const remove = async (uid: string) => {
    await api(projectPath(pid, `/members/${uid}`), { method: "DELETE" });
    void qc.invalidateQueries({ queryKey: ["members", pid] });
  };
  return (
    <div className="panel">
      <h2>Members</h2>
      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Name</th>
            <th>Role</th>
            {isAdmin && <th />}
          </tr>
        </thead>
        <tbody>
          {members.data?.map((m) => (
            <tr key={m.user_id}>
              <td>{m.email}</td>
              <td>{m.name}</td>
              <td><span className="badge">{m.role}</span></td>
              {isAdmin && (
                <td>
                  <button className="danger" onClick={() => remove(m.user_id)}>Remove</button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {isAdmin && (
        <div className="row" style={{ marginTop: 10 }}>
          <input type="email" placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} aria-label="Role">
            <option value="viewer">viewer</option>
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
          <input type="password" placeholder="Initial password (new users)" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <button className="primary" disabled={!form.email} onClick={add}>Add member</button>
        </div>
      )}
      {err && <div className="error-box">{err}</div>}
    </div>
  );
}

function DataSettings({ pid }: { pid: string }) {
  const qc = useQueryClient();
  const project = useQuery({ queryKey: ["project", pid], queryFn: () => api<Project>(projectPath(pid)) });
  const s = project.data?.settings ?? {};
  const [patterns, setPatterns] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const save = async (patch: Record<string, unknown>) => {
    setMsg("");
    try {
      await api(projectPath(pid, "/settings"), { method: "PUT", json: patch });
      void qc.invalidateQueries({ queryKey: ["project", pid] });
      setMsg("Saved");
    } catch (e) {
      setMsg(String(e));
    }
  };
  const patternText = patterns ?? (s.redact_patterns ?? []).map((p) => `${p.pattern} => ${p.label ?? "[REDACTED]"}`).join("\n");
  return (
    <div className="panel">
      <h2>Data</h2>
      <div className="grid">
        <label className="row">
          <input type="checkbox" checked={!!s.redact} onChange={(e) => save({ redact: e.target.checked })} />
          Redact PII (emails, phones, cards, API keys, IPs, IBANs) in inputs and outputs before they are stored
        </label>
        <div>
          <div className="small muted">Extra redaction patterns, one per line: <code>regex =&gt; [LABEL]</code></div>
          <textarea rows={3} value={patternText} onChange={(e) => setPatterns(e.target.value)} />
          <button
            onClick={() =>
              save({
                redact_patterns: patternText
                  .split("\n")
                  .filter((l) => l.trim())
                  .map((l) => {
                    const [pattern, label] = l.split("=>").map((x) => x.trim());
                    return { pattern, label: label || "[REDACTED]" };
                  }),
              })
            }
          >
            Save patterns
          </button>
        </div>
        <label className="row">
          Keep traces for
          <input
            type="number"
            min={1}
            placeholder="forever"
            defaultValue={s.retention_days ?? ""}
            onBlur={(e) => save({ retention_days: e.target.value ? Number(e.target.value) : null })}
            style={{ width: 90 }}
          />
          days (older traces are deleted by the worker)
        </label>
        <label className="row">
          Online-eval judge budget
          <input
            type="number"
            min={0}
            step="0.5"
            placeholder="none"
            defaultValue={s.judge_budget_usd_per_day ?? ""}
            onBlur={(e) => save({ judge_budget_usd_per_day: e.target.value ? Number(e.target.value) : null })}
            style={{ width: 90 }}
          />
          USD per day
        </label>
        {msg && <div className="small muted">{msg}</div>}
      </div>
    </div>
  );
}

function Prices({ pid, isAdmin }: { pid: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const prices = useQuery({ queryKey: ["prices", pid], queryFn: () => api<Record<string, Record<string, number>>>(projectPath(pid, "/prices")) });
  const [model, setModel] = useState("");
  const [inp, setInp] = useState("");
  const [out, setOut] = useState("");
  const save = async () => {
    await api(projectPath(pid, `/prices/${encodeURIComponent(model)}`), {
      method: "PUT",
      json: { input_tokens: Number(inp) / 1e6, output_tokens: Number(out) / 1e6 },
    });
    setModel("");
    void qc.invalidateQueries({ queryKey: ["prices", pid] });
  };
  return (
    <div className="panel">
      <h2>Model prices</h2>
      <p className="muted small">
        Costs use a built-in table of 5,500+ models (tokens, audio seconds, characters, images). Add your own models or negotiated prices here.
      </p>
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Prices (USD per unit)</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(prices.data ?? {}).map(([m, p]) => (
            <tr key={m}>
              <td className="mono">{m}</td>
              <td className="small mono">{Object.entries(p).map(([k, v]) => `${k}: ${v}`).join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {isAdmin && (
        <div className="row" style={{ marginTop: 10 }}>
          <input placeholder="model name" value={model} onChange={(e) => setModel(e.target.value)} />
          <input placeholder="$ / 1M input tokens" value={inp} onChange={(e) => setInp(e.target.value)} style={{ width: 150 }} />
          <input placeholder="$ / 1M output tokens" value={out} onChange={(e) => setOut(e.target.value)} style={{ width: 150 }} />
          <button className="primary" disabled={!model} onClick={save}>Save price</button>
        </div>
      )}
    </div>
  );
}
