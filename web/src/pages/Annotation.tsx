import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type Json, type Span, type Trace } from "../api";
import { usePid, useRole } from "../hooks";
import { Content } from "../components/Content";

interface Config {
  name: string;
  data_type: "numeric" | "categorical" | "boolean";
  min_value: number | null;
  max_value: number | null;
  categories: string[] | null;
  description: string | null;
}
interface Queue {
  queue_id: string;
  name: string;
  description: string | null;
  score_names: string[];
  n_items: number;
  n_done: number;
}

export function Annotation() {
  const pid = usePid();
  const role = useRole();
  const qc = useQueryClient();
  const configs = useQuery({ queryKey: ["configs", pid], queryFn: () => api<Config[]>(projectPath(pid, "/score-configs")) });
  const queues = useQuery({ queryKey: ["queues", pid], queryFn: () => api<Queue[]>(projectPath(pid, "/queues")) });
  const [cfg, setCfg] = useState({ name: "", data_type: "numeric", min_value: "1", max_value: "5", categories: "", description: "" });
  const [queue, setQueue] = useState({ name: "", scores: [] as string[], fill: "50" });
  const [err, setErr] = useState("");
  const canEdit = role !== "viewer";
  const saveConfig = async () => {
    setErr("");
    try {
      await api(projectPath(pid, `/score-configs/${encodeURIComponent(cfg.name)}`), {
        method: "PUT",
        json: {
          data_type: cfg.data_type,
          min_value: cfg.data_type === "numeric" ? Number(cfg.min_value) : null,
          max_value: cfg.data_type === "numeric" ? Number(cfg.max_value) : null,
          categories: cfg.data_type === "categorical" ? cfg.categories.split(",").map((s) => s.trim()).filter(Boolean) : null,
          description: cfg.description || null,
        },
      });
      setCfg({ ...cfg, name: "", description: "" });
      void qc.invalidateQueries({ queryKey: ["configs", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  const createQueue = async () => {
    setErr("");
    try {
      const r = await api<{ queue_id: string }>(projectPath(pid, "/queues"), { method: "POST", json: { name: queue.name, score_names: queue.scores } });
      if (Number(queue.fill) > 0) {
        await api(projectPath(pid, `/queues/${r.queue_id}/items`), { method: "POST", json: { filter: {}, limit: Number(queue.fill) } });
      }
      setQueue({ name: "", scores: [], fill: "50" });
      void qc.invalidateQueries({ queryKey: ["queues", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  return (
    <div style={{ maxWidth: 1000 }}>
      <div className="page-head">
        <div>
          <h1>Annotation</h1>
          <div className="muted small">Human labels: the ground truth for calibrating LLM judges and for reports.</div>
        </div>
      </div>
      {err && <div className="error-box" style={{ marginBottom: 10 }}>{err}</div>}
      <div className="grid">
        <div className="panel">
          <h2>Queues</h2>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Scores</th>
                <th className="num">Done</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {queues.data?.map((q) => (
                <tr key={q.queue_id}>
                  <td><b>{q.name}</b></td>
                  <td className="small">{q.score_names.join(", ")}</td>
                  <td className="num">
                    {q.n_done} / {q.n_items}
                  </td>
                  <td className="row">
                    <Link className="btn" to={`/p/${pid}/annotate/${q.queue_id}`}>Label</Link>
                    <a className="btn" href={projectPath(pid, `/queues/${q.queue_id}/labels.jsonl`)}>Export labels</a>
                  </td>
                </tr>
              ))}
              {queues.data && !queues.data.length && (
                <tr>
                  <td colSpan={4} className="empty">No queues yet. Define a score below, then create a queue.</td>
                </tr>
              )}
            </tbody>
          </table>
          {canEdit && (
            <div className="row" style={{ marginTop: 10 }}>
              <input placeholder="Queue name" value={queue.name} onChange={(e) => setQueue({ ...queue, name: e.target.value })} />
              <select
                multiple
                aria-label="Scores to collect"
                value={queue.scores}
                onChange={(e) => setQueue({ ...queue, scores: [...e.target.selectedOptions].map((o) => o.value) })}
                style={{ minWidth: 160 }}
              >
                {configs.data?.map((c) => <option key={c.name}>{c.name}</option>)}
              </select>
              <label className="small">
                fill with latest
                <input type="number" min={0} value={queue.fill} onChange={(e) => setQueue({ ...queue, fill: e.target.value })} style={{ width: 70, marginLeft: 6 }} />
                traces
              </label>
              <button className="primary" disabled={!queue.name || !queue.scores.length} onClick={createQueue}>Create queue</button>
            </div>
          )}
        </div>
        <div className="panel">
          <h2>Score definitions</h2>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Values</th>
                <th>Guidance</th>
              </tr>
            </thead>
            <tbody>
              {configs.data?.map((c) => (
                <tr key={c.name}>
                  <td><b>{c.name}</b></td>
                  <td>{c.data_type}</td>
                  <td className="small">
                    {c.data_type === "numeric" ? `${c.min_value}–${c.max_value}` : c.data_type === "boolean" ? "true / false" : c.categories?.join(", ")}
                  </td>
                  <td className="small">{c.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {canEdit && (
            <div className="row" style={{ marginTop: 10 }}>
              <input placeholder="Score name (e.g. correctness)" value={cfg.name} onChange={(e) => setCfg({ ...cfg, name: e.target.value })} />
              <select value={cfg.data_type} onChange={(e) => setCfg({ ...cfg, data_type: e.target.value })} aria-label="Type">
                <option value="numeric">numeric</option>
                <option value="boolean">pass / fail</option>
                <option value="categorical">categories</option>
              </select>
              {cfg.data_type === "numeric" && (
                <>
                  <input aria-label="min" value={cfg.min_value} onChange={(e) => setCfg({ ...cfg, min_value: e.target.value })} style={{ width: 50 }} />–
                  <input aria-label="max" value={cfg.max_value} onChange={(e) => setCfg({ ...cfg, max_value: e.target.value })} style={{ width: 50 }} />
                </>
              )}
              {cfg.data_type === "categorical" && (
                <input placeholder="good, partial, bad" value={cfg.categories} onChange={(e) => setCfg({ ...cfg, categories: e.target.value })} />
              )}
              <input placeholder="Guidance for labellers" value={cfg.description} onChange={(e) => setCfg({ ...cfg, description: e.target.value })} style={{ flex: 1 }} />
              <button className="primary" disabled={!cfg.name} onClick={saveConfig}>Save score</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface NextResponse {
  queue: Queue;
  configs: Config[];
  item: { item_id: string; trace_id: string } | null;
  trace?: Trace;
  spans?: Span[];
}

function options(c: Config): { label: string; value: string | number | boolean }[] {
  if (c.data_type === "boolean") return [{ label: "pass", value: true }, { label: "fail", value: false }];
  if (c.data_type === "categorical") return (c.categories ?? []).map((x) => ({ label: x, value: x }));
  const lo = c.min_value ?? 1;
  const hi = c.max_value ?? 5;
  if (Number.isInteger(lo) && Number.isInteger(hi) && hi - lo <= 9) {
    return Array.from({ length: hi - lo + 1 }, (_, i) => ({ label: String(lo + i), value: lo + i }));
  }
  return [];
}

/** One trace at a time; number keys pick values, Enter submits, S skips. */
export function Labeller() {
  const pid = usePid();
  const { qid = "" } = useParams();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["next", pid, qid],
    queryFn: () => api<NextResponse>(projectPath(pid, `/queues/${qid}/next`)),
    staleTime: 0,
  });
  const [values, setValues] = useState<Record<string, string | number | boolean>>({});
  const [focus, setFocus] = useState(0);
  const [comment, setComment] = useState("");
  const [err, setErr] = useState("");
  const [count, setCount] = useState(0);
  const configs = q.data?.configs ?? [];
  const item = q.data?.item;
  useEffect(() => {
    setValues({});
    setFocus(0);
    setComment("");
  }, [item?.item_id]);
  const submit = useCallback(async () => {
    if (!item) return;
    setErr("");
    try {
      await api(projectPath(pid, `/queues/${qid}/items/${item.item_id}/complete`), { method: "POST", json: { scores: values, comment: comment || null } });
      setCount((c) => c + 1);
      void qc.invalidateQueries({ queryKey: ["next", pid, qid] });
      void qc.invalidateQueries({ queryKey: ["queues", pid] });
    } catch (e) {
      setErr(String(e));
    }
  }, [item, values, comment, pid, qid, qc]);
  const skip = useCallback(async () => {
    if (!item) return;
    await api(projectPath(pid, `/queues/${qid}/items/${item.item_id}/skip`), { method: "POST" });
    void qc.invalidateQueries({ queryKey: ["next", pid, qid] });
  }, [item, pid, qid, qc]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === "TEXTAREA" || (e.target as HTMLElement).tagName === "INPUT") return;
      const c = configs[focus];
      if (!c) return;
      const opts = options(c);
      const n = Number(e.key);
      if (!Number.isNaN(n) && e.key !== " ") {
        const pick = c.data_type === "numeric" ? opts.find((o) => o.value === n) : opts[n - 1];
        if (pick) {
          setValues((v) => ({ ...v, [c.name]: pick.value }));
          if (focus < configs.length - 1) setFocus(focus + 1);
        }
      } else if (e.key === "Enter") {
        void submit();
      } else if (e.key.toLowerCase() === "s") {
        void skip();
      } else if (e.key === "Tab") {
        e.preventDefault();
        setFocus((focus + (e.shiftKey ? configs.length - 1 : 1)) % configs.length);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [configs, focus, submit, skip]);
  if (!q.data) return <div className="muted">Loading…</div>;
  const queue = q.data.queue;
  if (!item)
    return (
      <div className="empty">
        Queue “{queue.name}” is done ({queue.n_done} labelled). <Link to={`/p/${pid}/annotation`}>Back to queues</Link>
      </div>
    );
  const llm = (q.data.spans ?? []).filter((s) => s.kind === "llm");
  const complete = configs.every((c) => values[c.name] !== undefined);
  return (
    <div className="label-card">
      <div className="small muted">
        <Link to={`/p/${pid}/annotation`}>Annotation</Link> / {queue.name} · {queue.n_done} of {queue.n_items} done · {count} this session
      </div>
      <div className="page-head">
        <h1>{q.data.trace?.name}</h1>
        <Link className="small" to={`/p/${pid}/traces/${item.trace_id}`} target="_blank">Open full trace</Link>
      </div>
      <div className="grid grid-2">
        <div className="panel">
          <h3>Input</h3>
          <Content value={(q.data.trace?.input ?? null) as Json} pid={pid} />
          {llm.length > 0 && (
            <details style={{ marginTop: 10 }}>
              <summary className="small muted">Model calls ({llm.length})</summary>
              {llm.map((s) => (
                <div key={s.span_id} style={{ marginTop: 8 }}>
                  <div className="small"><b>{s.name}</b> {s.model}</div>
                  <Content value={s.input} pid={pid} />
                </div>
              ))}
            </details>
          )}
        </div>
        <div className="panel">
          <h3>Output</h3>
          <Content value={(q.data.trace?.output ?? null) as Json} pid={pid} />
        </div>
      </div>
      <div className="panel" style={{ marginTop: 14 }}>
        {configs.map((c, i) => (
          <div key={c.name} style={{ marginBottom: 10, padding: 6, borderRadius: 6, background: i === focus ? "var(--accent-soft)" : undefined }} onClick={() => setFocus(i)}>
            <div className="row">
              <b>{c.name}</b>
              <span className="small muted">{c.description}</span>
            </div>
            <div className="row" style={{ marginTop: 4 }}>
              {options(c).map((o, j) => (
                <button key={String(o.value)} className={values[c.name] === o.value ? "primary" : ""} onClick={() => setValues({ ...values, [c.name]: o.value })}>
                  <span className="kbd">{c.data_type === "numeric" ? String(o.value) : j + 1}</span> {o.label}
                </button>
              ))}
              {c.data_type === "numeric" && !options(c).length && (
                <input type="number" min={c.min_value ?? undefined} max={c.max_value ?? undefined} onChange={(e) => setValues({ ...values, [c.name]: Number(e.target.value) })} />
              )}
            </div>
          </div>
        ))}
        <textarea rows={2} placeholder="Comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} style={{ fontFamily: "inherit" }} />
        {err && <div className="error-box">{err}</div>}
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" disabled={!complete} onClick={submit}>
            Submit <span className="kbd">Enter</span>
          </button>
          <button onClick={skip}>
            Skip <span className="kbd">S</span>
          </button>
          <span className="small muted">Number keys pick a value · Tab moves between scores</span>
        </div>
      </div>
    </div>
  );
}
