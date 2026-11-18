import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type AlertList, type Evaluator, type EvaluatorList } from "../api";
import { usePid, useRole } from "../hooks";
import { fmtAgo, fmtCost, fmtNum } from "../format";
import { EmptyState } from "../components/EmptyState";

export function OnlineEvals() {
  const pid = usePid();
  const role = useRole();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["evaluators", pid],
    queryFn: () => api<EvaluatorList>(projectPath(pid, "/evaluators")),
    refetchInterval: 10_000,
  });
  const [form, setForm] = useState({ name: "", spec: "judge:correctness", model: "", rate: "10", traceName: "", backfill: "0" });
  const [err, setErr] = useState("");
  const create = async () => {
    setErr("");
    try {
      await api(projectPath(pid, "/evaluators"), {
        method: "POST",
        json: {
          name: form.name,
          scorer: { spec: form.spec, model: form.model || undefined },
          sampling_rate: Number(form.rate) / 100,
          filter: form.traceName ? { name: form.traceName } : {},
          backfill_s: Number(form.backfill) * 3600,
        },
      });
      setForm({ ...form, name: "" });
      void qc.invalidateQueries({ queryKey: ["evaluators", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  const toggle = async (e: Evaluator) => {
    await api(projectPath(pid, `/evaluators/${e.evaluator_id}`), { method: "PATCH", json: { active: !e.active } });
    void qc.invalidateQueries({ queryKey: ["evaluators", pid] });
  };
  return (
    <div style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1>Online evals</h1>
          <div className="muted small">
            Score a deterministic sample of production traces as they arrive. Scores appear on traces and in the dashboard. Judge spend today: <b>{fmtCost(q.data?.spend_today_usd ?? 0)}</b> (cap it in
            Settings).
          </div>
        </div>
      </div>
      <div className="panel" style={{ padding: 0, marginBottom: 14 }}>
        <table>
          <thead>
            <tr>
              <th>Evaluator</th>
              <th>Scorer</th>
              <th>Traces</th>
              <th className="num">Sample</th>
              <th className="num">Scored</th>
              <th className="num">Failed</th>
              <th className="num">Skipped (budget)</th>
              <th>Up to</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {q.data?.evaluators.map((e) => (
              <tr key={e.evaluator_id}>
                <td>
                  <b>{e.name}</b> {!e.active && <span className="badge">paused</span>}
                </td>
                <td className="small mono">
                  {e.scorer.spec}
                  {e.scorer.model && <div className="muted">{e.scorer.model}</div>}
                </td>
                <td className="small">{Object.entries(e.filter).map(([k, v]) => `${k}=${v}`).join(", ") || "all"}</td>
                <td className="num">{Math.round(e.sampling_rate * 100)}%</td>
                <td className="num">{fmtNum(e.n_scored)}</td>
                <td className="num">{fmtNum(e.n_failed)}</td>
                <td className="num">{fmtNum(e.n_skipped_budget)}</td>
                <td className="small muted">{fmtAgo(e.cursor)}</td>
                <td>{role !== "viewer" && <button onClick={() => toggle(e)}>{e.active ? "Pause" : "Resume"}</button>}</td>
              </tr>
            ))}
            {q.data && !q.data.evaluators.length && (
              <tr>
                <td colSpan={9}>
                  <EmptyState title="Score live traffic automatically">
                    <p>An evaluator scores a sample of new traces as they arrive, so quality drops show up without you looking. Judge calls cost money; set a daily cap in Settings.</p>
                    {role !== "viewer" && (
                      <button className="primary" onClick={() => { setForm({ ...form, name: form.name || "quality", spec: "judge:correctness", rate: "10" }); document.getElementById("new-evaluator")?.scrollIntoView({ block: "center" }); setTimeout(() => document.getElementById("evaluator-model")?.focus(), 0); }}>
                        Set up a correctness judge on 10% of traces
                      </button>
                    )}
                  </EmptyState>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {role !== "viewer" && (
        <div className="panel">
          <h2 id="new-evaluator">New evaluator</h2>
          <div className="row">
            <input placeholder="Score name (e.g. quality)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <select value={form.spec} onChange={(e) => setForm({ ...form, spec: e.target.value })} aria-label="Scorer">
              {["judge:correctness", "judge:faithfulness", "judge:answer_relevance", "judge:helpfulness", "judge:toxicity", "judge:conciseness", "json_valid"].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
            {form.spec.startsWith("judge:") && (
              <input id="evaluator-model" placeholder="judge model" aria-label="Judge model" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
            )}
            <label className="small">
              sample <input type="number" min={1} max={100} value={form.rate} onChange={(e) => setForm({ ...form, rate: e.target.value })} style={{ width: 60 }} />%
            </label>
            <input placeholder="only trace name (optional)" value={form.traceName} onChange={(e) => setForm({ ...form, traceName: e.target.value })} />
            <label className="small">
              backfill <input type="number" min={0} value={form.backfill} onChange={(e) => setForm({ ...form, backfill: e.target.value })} style={{ width: 60 }} /> h
            </label>
            <button className="primary" disabled={!form.name || (form.spec.startsWith("judge:") && !form.model)} onClick={create}>Create</button>
          </div>
          {err && <div className="error-box" style={{ marginTop: 8 }}>{err}</div>}
        </div>
      )}
    </div>
  );
}

export function AlertsPage() {
  const pid = usePid();
  const role = useRole();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["alerts", pid],
    queryFn: () => api<AlertList>(projectPath(pid, "/alerts")),
    refetchInterval: 15_000,
  });
  const [f, setF] = useState({ name: "", metric: "error_rate", op: ">", threshold: "0.05", window: "60", score: "", channel: "webhook", target: "" });
  const [err, setErr] = useState("");
  const create = async () => {
    setErr("");
    try {
      await api(projectPath(pid, "/alerts"), {
        method: "POST",
        json: {
          name: f.name,
          rule: { metric: f.metric, op: f.op, threshold: Number(f.threshold), window_s: Number(f.window) * 60, score_name: f.score || undefined },
          channel: f.channel === "webhook" ? { type: "webhook", url: f.target } : { type: "email", to: f.target },
        },
      });
      void qc.invalidateQueries({ queryKey: ["alerts", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  const check = async (id: string) => {
    await api(projectPath(pid, `/alerts/${id}/check`), { method: "POST" });
    void qc.invalidateQueries({ queryKey: ["alerts", pid] });
  };
  const names = new Map(q.data?.alerts.map((a) => [a.alert_id, a.name]));
  return (
    <div style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1>Alerts</h1>
          <div className="muted small">Checked every minute by the worker; notifications are sent when an alert starts firing and when it resolves.</div>
        </div>
      </div>
      <div className="panel" style={{ padding: 0, marginBottom: 14 }}>
        <table>
          <thead>
            <tr>
              <th>Alert</th>
              <th>Rule</th>
              <th>State</th>
              <th className="num">Last value</th>
              <th>Notify</th>
              <th>Checked</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {q.data?.alerts.map((a) => (
              <tr key={a.alert_id}>
                <td><b>{a.name}</b></td>
                <td className="small mono">
                  {a.rule.metric}
                  {a.rule.score_name ? `(${a.rule.score_name})` : ""} {a.rule.op} {a.rule.threshold} over {Math.round((a.rule.window_s ?? 3600) / 60)} min
                </td>
                <td><span className={`badge ${a.state === "firing" ? "bad" : "good"}`}>{a.state}</span></td>
                <td className="num">{a.last_value === null ? "—" : fmtNum(a.last_value, 3)}</td>
                <td className="small">{a.channel.type}: {a.channel.url ?? a.channel.to}</td>
                <td className="small muted">{fmtAgo(a.last_checked)}</td>
                <td><button onClick={() => check(a.alert_id)}>Check now</button></td>
              </tr>
            ))}
            {q.data && !q.data.alerts.length && (
              <tr>
                <td colSpan={7} className="empty">No alerts yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {role === "admin" && (
        <div className="panel" style={{ marginBottom: 14 }}>
          <h2>New alert</h2>
          <div className="row">
            <input placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
            <select value={f.metric} onChange={(e) => setF({ ...f, metric: e.target.value })} aria-label="Metric">
              <option value="error_rate">error rate (0–1)</option>
              <option value="p95_latency_ms">p95 latency (ms)</option>
              <option value="cost_usd">cost (USD)</option>
              <option value="trace_count">trace count</option>
              <option value="score_mean">score average (fires when its 95% interval is past the threshold)</option>
            </select>
            {f.metric === "score_mean" && <input placeholder="score name" value={f.score} onChange={(e) => setF({ ...f, score: e.target.value })} />}
            <select value={f.op} onChange={(e) => setF({ ...f, op: e.target.value })} aria-label="Operator">
              <option>&gt;</option>
              <option>&lt;</option>
            </select>
            <input aria-label="Threshold" value={f.threshold} onChange={(e) => setF({ ...f, threshold: e.target.value })} style={{ width: 80 }} />
            <label className="small">over <input value={f.window} onChange={(e) => setF({ ...f, window: e.target.value })} style={{ width: 60 }} /> min</label>
            <select value={f.channel} onChange={(e) => setF({ ...f, channel: e.target.value })} aria-label="Channel">
              <option value="webhook">webhook / Slack</option>
              <option value="email">email</option>
            </select>
            <input placeholder={f.channel === "webhook" ? "https://hooks.slack.com/…" : "ops@example.com"} value={f.target} onChange={(e) => setF({ ...f, target: e.target.value })} style={{ flex: 1 }} />
            <button className="primary" disabled={!f.name || !f.target} onClick={create}>Create</button>
          </div>
          {err && <div className="error-box" style={{ marginTop: 8 }}>{err}</div>}
        </div>
      )}
      <div className="panel">
        <h2>History</h2>
        <table>
          <tbody>
            {q.data?.events.map((e, i) => (
              <tr key={i}>
                <td className="small muted">{fmtAgo(e.at)}</td>
                <td>{names.get(e.alert_id)}</td>
                <td><span className={`badge ${e.state === "firing" ? "bad" : "good"}`}>{e.state}</span></td>
                <td className="num">{e.value === null ? "—" : fmtNum(e.value, 3)}</td>
                <td className="small">{e.delivered ? "delivered" : <span className="badge warn">not delivered: {e.error}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
