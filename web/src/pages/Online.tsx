import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type AlertList, type Evaluator, type EvaluatorList, type JudgeReports, type Json, type Queue } from "../api";
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
              <option value="score_mean">score average</option>
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

export function JudgeReliability() {
  const pid = usePid();
  const role = useRole();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["judge-reports", pid],
    queryFn: () => api<JudgeReports>(projectPath(pid, "/judge-reports")),
    refetchInterval: 10_000,
  });
  const queues = useQuery({ queryKey: ["queues", pid], queryFn: () => api<Queue[]>(projectPath(pid, "/queues")) });
  const [f, setF] = useState({ queue: "", label: "", rubric: "correctness", text: "", model: "" });
  const [msg, setMsg] = useState("");
  const rubricText = f.text || q.data?.builtin[f.rubric] || "";
  const queue = queues.data?.find((x) => x.queue_id === f.queue);
  const start = async () => {
    setMsg("");
    try {
      await api(projectPath(pid, "/judge-reports"), {
        method: "POST",
        json: { queue_id: f.queue, label: f.label, rubric_name: f.rubric, rubric_text: rubricText, model: f.model },
      });
      setMsg("Running… the report appears below when the worker finishes.");
      void qc.invalidateQueries({ queryKey: ["judge-reports", pid] });
    } catch (e) {
      setMsg(String(e));
    }
  };
  const n = (v: number | null | string | undefined, d = 3) => (typeof v === "number" ? v.toFixed(d) : "—");
  return (
    <div style={{ maxWidth: 1150 }}>
      <div className="page-head">
        <div>
          <h1>Judge reliability</h1>
          <div className="muted small">
            Can the LLM judge be trusted? Each report runs a judge over human labels from an annotation queue: Cohen's kappa (agreement beyond chance), position-swap flip rate and length
            bias. Every rubric version keeps its numbers, so calibration progress is visible.
          </div>
        </div>
      </div>
      <div className="panel" style={{ padding: 0, marginBottom: 14 }}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Rubric</th>
                <th>Judge model</th>
                <th className="num">Items</th>
                <th className="num">κ</th>
                <th className="num">Weighted κ</th>
                <th className="num">Pass/fail κ</th>
                <th className="num">Agreement</th>
                <th className="num">Flip rate</th>
                <th className="num">Length corr. judge / human</th>
              </tr>
            </thead>
            <tbody>
              {q.data?.reports.map((r) => (
                <tr key={r.report_id}>
                  <td className="small muted">{fmtAgo(r.created)}</td>
                  <td>
                    {r.rubric_name} <span className="mono small muted">@{r.rubric_version}</span>
                  </td>
                  <td className="small">{r.model}</td>
                  <td className="num">{String(r.summary.n_judged ?? "—")}</td>
                  <td className="num"><b>{n(r.summary.kappa)}</b></td>
                  <td className="num">{n(r.summary.weighted_kappa)}</td>
                  <td className="num">{n(r.summary.pass_fail_kappa)}</td>
                  <td className="num">{n(r.summary.exact_agreement, 2)}</td>
                  <td className="num">
                    {n(r.summary.flip_rate, 2)} <span className="small muted">n={String(r.summary.n_pairs_decided ?? 0)}</span>
                  </td>
                  <td className="num">
                    {n(r.summary.length_corr_judge, 2)} / {n(r.summary.length_corr_human, 2)}
                  </td>
                </tr>
              ))}
              {q.data && !q.data.reports.length && (
                <tr>
                  <td colSpan={10} className="empty">No reports yet. Label outputs in an annotation queue, then run the judge against them below.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      {role !== "viewer" && (
        <div className="panel">
          <h2>Run a judge against human labels</h2>
          <div className="row" style={{ marginBottom: 8 }}>
            <select value={f.queue} onChange={(e) => setF({ ...f, queue: e.target.value, label: "" })} aria-label="Queue">
              <option value="">labels from queue…</option>
              {queues.data?.map((x) => <option key={x.queue_id} value={x.queue_id}>{x.name} ({x.n_done} labelled)</option>)}
            </select>
            <select value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} aria-label="Human label">
              <option value="">human score…</option>
              {queue?.score_names.map((s) => <option key={s}>{s}</option>)}
            </select>
            <select value={f.rubric} onChange={(e) => setF({ ...f, rubric: e.target.value, text: "" })} aria-label="Rubric">
              {Object.keys(q.data?.builtin ?? {}).map((r) => <option key={r}>{r}</option>)}
            </select>
            <input placeholder="judge model" value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} />
            <button className="primary" disabled={!f.queue || !f.label || !f.model} onClick={start}>Run report</button>
          </div>
          <div className="small muted">Rubric text (edit to calibrate; a changed text is a new rubric version)</div>
          <textarea rows={7} value={rubricText} onChange={(e) => setF({ ...f, text: e.target.value })} style={{ fontFamily: "inherit" }} />
          {msg && <div className="small muted" style={{ marginTop: 6 }}>{msg}</div>}
          <JsonHint />
        </div>
      )}
    </div>
  );
}

function JsonHint() {
  const text: Json = "CLI: tirkit judge-report labels.jsonl --label correct --rubric correctness --model <judge>";
  return <div className="small muted mono" style={{ marginTop: 8 }}>{String(text)}</div>;
}
