import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type Json, type Score, type Span, type TraceDetail } from "../api";
import { usePid, useRole } from "../hooks";
import { Content, JsonBlock } from "../components/Content";
import { AddToDataset } from "../components/AddToDataset";
import { fmtCost, fmtMs, fmtNum, fmtTime, kindLabel } from "../format";

interface Node {
  span: Span;
  depth: number;
}

function buildTree(spans: Span[]): Node[] {
  const children = new Map<string | null, Span[]>();
  const ids = new Set(spans.map((s) => s.span_id));
  for (const s of spans) {
    const parent = s.parent_id && ids.has(s.parent_id) ? s.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), s]);
  }
  const out: Node[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const s of (children.get(parent) ?? []).sort((a, b) => a.start - b.start)) {
      out.push({ span: s, depth });
      walk(s.span_id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

export default function TraceView() {
  const pid = usePid();
  const { tid = "" } = useParams();
  const role = useRole();
  const q = useQuery({ queryKey: ["trace", pid, tid], queryFn: () => api<TraceDetail>(projectPath(pid, `/traces/${tid}`)) });
  const [sel, setSel] = useState<string | null>(null);
  const nodes = useMemo(() => buildTree(q.data?.spans ?? []), [q.data]);
  if (q.isLoading) return <div className="muted">Loading…</div>;
  if (q.error || !q.data) return <div className="error-box">{String(q.error ?? "not found")}</div>;
  const { trace, scores } = q.data;
  const t0 = Math.min(...nodes.map((n) => n.span.start));
  const t1 = Math.max(...nodes.map((n) => n.span.end ?? n.span.start));
  const total = Math.max(t1 - t0, 1e-6);
  const selected = nodes.find((n) => n.span.span_id === sel)?.span ?? nodes[0]?.span;

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="small muted">
            <Link to={`/p/${pid}/traces`}>Traces</Link> / <span className="mono">{trace.trace_id}</span>
          </div>
          <h1>
            {trace.name} {trace.has_error && <span className="badge bad">error</span>}
          </h1>
          <div className="row small muted">
            <span>{fmtTime(trace.start)}</span>·<span>{fmtMs(trace.duration_ms)}</span>·<span>{trace.n_spans} spans</span>·
            <span>{fmtNum((trace.input_tokens ?? 0) + (trace.output_tokens ?? 0))} tokens</span>·<span>{fmtCost(trace.cost_usd)}</span>
            {trace.session_id && (
              <>
                · <Link to={`/p/${pid}/sessions/${encodeURIComponent(trace.session_id)}`}>session {trace.session_id}</Link>
              </>
            )}
            {trace.user_id && <>· user {trace.user_id}</>}
            {trace.release && <>· release {trace.release}</>}
            {trace.tags.map((t) => (
              <span key={t} className="badge">{t}</span>
            ))}
          </div>
        </div>
        {role !== "viewer" && <AddToDataset traceIds={[trace.trace_id]} />}
      </div>
      <div className="split">
        <div className="panel tree" style={{ padding: 8 }}>
          {nodes.map(({ span: s, depth }) => {
            const left = ((s.start - t0) / total) * 100;
            const width = Math.max((((s.end ?? s.start) - s.start) / total) * 100, 0.5);
            return (
              <div key={s.span_id} className={`tree-row ${selected?.span_id === s.span_id ? "selected" : ""}`} onClick={() => setSel(s.span_id)}>
                <div className="tree-name" style={{ paddingLeft: depth * 14 }}>
                  <span className="kind" title={s.kind}>{kindLabel(s.kind)}</span>
                  {s.name}
                  {s.status === "error" && <span className="badge bad" style={{ marginLeft: 6 }}>error</span>}
                </div>
                <div>
                  <div className="bar-track">
                    <div className={`bar ${s.status === "error" ? "error" : ""}`} style={{ left: `${left}%`, width: `${width}%` }} />
                  </div>
                  <div className="small muted" style={{ textAlign: "right" }}>{fmtMs(s.end ? (s.end - s.start) * 1000 : null)}</div>
                </div>
              </div>
            );
          })}
        </div>
        <div className="grid">
          {selected && <SpanPanel span={selected} pid={pid} />}
          <ScoresPanel scores={scores} traceId={trace.trace_id} spanId={selected?.span_id ?? null} canEdit={role !== "viewer"} />
        </div>
      </div>
    </div>
  );
}

function SpanPanel({ span: s, pid }: { span: Span; pid: string }) {
  const [tab, setTab] = useState<"io" | "meta">("io");
  const nav = useNavigate();
  const messages = (s.input && typeof s.input === "object" && !Array.isArray(s.input) && Array.isArray((s.input as Record<string, unknown>).messages))
    ? ((s.input as Record<string, unknown>).messages as Json[])
    : null;
  const replay = () => {
    sessionStorage.setItem("tirkit.playground", JSON.stringify({ kind: "chat", template: messages, model: s.model ?? "", params: s.model_params }));
    nav(`/p/${pid}/playground`);
  };
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>{s.name}</h2>
        <span className="kind">{s.kind.replace(/_/g, " ")}</span>
        {s.model && <span className="badge accent">{s.model}</span>}
        <div className="spacer" />
        {s.kind === "llm" && messages && <button onClick={replay}>Open in playground</button>}
        <span className="small muted">
          {fmtMs(s.end ? (s.end - s.start) * 1000 : null)}
          {s.ttft_ms !== null && ` · first token ${fmtMs(s.ttft_ms)}`} · {fmtCost(s.cost_usd)}
        </span>
      </div>
      {s.error && <div className="error-box" style={{ marginBottom: 8 }}>{s.error}</div>}
      <div className="tabs">
        <button className={tab === "io" ? "on" : ""} onClick={() => setTab("io")}>Input / output</button>
        <button className={tab === "meta" ? "on" : ""} onClick={() => setTab("meta")}>Details</button>
      </div>
      {tab === "io" ? (
        <div className="grid">
          <div>
            <h3>Input</h3>
            <Content value={s.input} pid={pid} />
          </div>
          <div>
            <h3>Output</h3>
            <Content value={s.output} pid={pid} />
          </div>
        </div>
      ) : (
        <div className="grid">
          <dl className="kv">
            <dt>Span id</dt>
            <dd className="mono">{s.span_id}</dd>
            <dt>Start</dt>
            <dd>{fmtTime(s.start)}</dd>
            <dt>Status</dt>
            <dd>{s.status}</dd>
            {Object.entries(s.usage).map(([k, v]) => (
              <Frag key={k} k={k} v={fmtNum(v, Number.isInteger(v) ? 0 : 2)} />
            ))}
            {s.prompt_version && <Frag k="prompt version" v={s.prompt_version} />}
          </dl>
          {Object.keys(s.model_params).length > 0 && (
            <div>
              <h3>Model parameters</h3>
              <JsonBlock value={s.model_params} />
            </div>
          )}
          {Object.keys(s.attributes).length > 0 && (
            <div>
              <h3>Attributes</h3>
              <JsonBlock value={s.attributes} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const Frag = ({ k, v }: { k: string; v: string }) => (
  <>
    <dt>{k}</dt>
    <dd>{v}</dd>
  </>
);

function ScoresPanel({ scores, traceId, spanId, canEdit }: { scores: Score[]; traceId: string; spanId: string | null; canEdit: boolean }) {
  const pid = usePid();
  const qc = useQueryClient();
  const [name, setName] = useState("quality");
  const [value, setValue] = useState("");
  const [comment, setComment] = useState("");
  const [err, setErr] = useState("");
  const add = async () => {
    setErr("");
    try {
      await api(projectPath(pid, "/scores"), {
        method: "POST",
        json: { trace_id: traceId, span_id: spanId, name, value: Number(value), comment: comment || null },
      });
      setValue("");
      setComment("");
      void qc.invalidateQueries({ queryKey: ["trace", pid, traceId] });
    } catch (e) {
      setErr(String(e));
    }
  };
  return (
    <div className="panel">
      <h3>Scores</h3>
      {scores.length ? (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th className="num">Value</th>
              <th>Source</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {scores.map((s) => (
              <tr key={s.score_id}>
                <td>{s.name}</td>
                <td className="num">{s.value ?? s.value_str ?? <span className="badge bad">failed</span>}</td>
                <td><span className="badge">{s.source}</span> {s.author && <span className="small muted">{s.author}</span>}</td>
                <td className="small">{s.reason ?? s.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="muted small">No scores yet.</div>
      )}
      {canEdit && (
        <div className="row" style={{ marginTop: 10 }}>
          <input aria-label="Score name" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 120 }} />
          <input aria-label="Score value" placeholder="value" type="number" value={value} onChange={(e) => setValue(e.target.value)} style={{ width: 80 }} />
          <input aria-label="Comment" placeholder="comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} style={{ flex: 1 }} />
          <button className="primary" disabled={value === "" || !name} onClick={add}>Add score</button>
        </div>
      )}
      {err && <div className="error-box">{err}</div>}
    </div>
  );
}
