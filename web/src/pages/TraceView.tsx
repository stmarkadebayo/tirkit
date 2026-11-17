import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type Json, type Score, type Span, type SpanSummary, type TraceDetail } from "../api";
import { usePid, useRole } from "../hooks";
import { Content, JsonBlock } from "../components/Content";
import { AddToDataset } from "../components/AddToDataset";
import { fmtCost, fmtMs, fmtNum, fmtTime, kindLabel } from "../format";
import { buildTree, type TreeNode } from "./traceTree";

const PAGE = 10_000; // spans per request; the server marks the rest as omitted
const ROW_H = 36; // fixed row height: only the rows in view are rendered
const OVERSCAN = 12;

const ISSUE_TITLE = {
  orphan: "Its parent span is not in this trace",
  "self-parent": "This span names itself as its parent",
  cycle: "Parent links form a cycle; shown from its earliest span",
} as const;

export default function TraceView() {
  const pid = usePid();
  const { tid = "" } = useParams();
  const role = useRole();
  const [limit, setLimit] = useState(PAGE);
  const q = useQuery({
    queryKey: ["trace", pid, tid, limit],
    queryFn: () => api<TraceDetail>(projectPath(pid, `/traces/${tid}?limit=${limit}`)),
    placeholderData: (prev) => prev,
  });
  const [sel, setSel] = useState<string | null>(null);
  const nodes = useMemo(() => buildTree(q.data?.spans ?? []), [q.data]);
  const range = useMemo(() => {
    let t0 = Infinity;
    let t1 = -Infinity;
    for (const { span: s } of nodes) {
      if (s.start < t0) t0 = s.start;
      const e = s.end ?? s.start;
      if (e > t1) t1 = e;
    }
    return { t0, total: Math.max(t1 - t0, 1e-6) };
  }, [nodes]);
  const selected = (sel ? nodes.find((n) => n.span.span_id === sel)?.span : undefined) ?? nodes[0]?.span;
  if (q.isLoading) return <div className="muted">Loading…</div>;
  if (q.error || !q.data) return <div className="error-box">{String(q.error ?? "not found")}</div>;
  const { trace, scores, omitted, total_spans } = q.data;

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
          <SpanTree nodes={nodes} t0={range.t0} total={range.total} selectedId={selected?.span_id} onSelect={setSel} />
          {omitted > 0 && (
            <div className="row small muted" style={{ padding: "8px 6px" }}>
              <span>
                Showing {fmtNum(nodes.length)} of {fmtNum(total_spans)} spans; {fmtNum(omitted)} omitted.
              </span>
              <button disabled={q.isFetching} onClick={() => setLimit(Math.min(total_spans, limit + PAGE))}>
                Show {fmtNum(Math.min(omitted, PAGE))} more
              </button>
            </div>
          )}
        </div>
        <div className="grid">
          {selected && <SelectedSpan summary={selected} pid={pid} tid={tid} />}
          <ScoresPanel scores={scores} traceId={trace.trace_id} spanId={selected?.span_id ?? null} canEdit={role !== "viewer"} />
        </div>
      </div>
    </div>
  );
}

/** The span tree, windowed: rows have a fixed height and only those in view are in the DOM. */
function SpanTree({
  nodes,
  t0,
  total,
  selectedId,
  onSelect,
}: {
  nodes: TreeNode<SpanSummary>[];
  t0: number;
  total: number;
  selectedId: string | undefined;
  onSelect: (id: string) => void;
}) {
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(800);
  const first = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const last = Math.min(nodes.length, Math.ceil((scrollTop + viewH) / ROW_H) + OVERSCAN);
  return (
    <div
      className="tree-scroll"
      style={{ height: nodes.length * ROW_H }}
      ref={(el) => {
        if (el && el.clientHeight && el.clientHeight !== viewH) setViewH(el.clientHeight);
      }}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
    >
      <div style={{ height: nodes.length * ROW_H, position: "relative" }}>
        {nodes.slice(first, last).map(({ span: s, depth, issue }, i) => {
          const left = ((s.start - t0) / total) * 100;
          const width = Math.max((((s.end ?? s.start) - s.start) / total) * 100, 0.5);
          return (
            <div
              key={s.span_id}
              className={`tree-row ${selectedId === s.span_id ? "selected" : ""}`}
              style={{ position: "absolute", top: (first + i) * ROW_H, left: 0, right: 0, height: ROW_H }}
              onClick={() => onSelect(s.span_id)}
            >
              <div className="tree-name" style={{ paddingLeft: Math.min(depth, 40) * 14 }} title={s.name}>
                <span className="kind" title={s.kind}>{kindLabel(s.kind)}</span>
                {s.name}
                {s.status === "error" && <span className="badge bad" style={{ marginLeft: 6 }}>error</span>}
                {issue && <span className="badge warn" style={{ marginLeft: 6 }} title={ISSUE_TITLE[issue]}>{issue}</span>}
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
    </div>
  );
}

/** Loads the selected span in full; shows its preview while it loads. */
function SelectedSpan({ summary, pid, tid }: { summary: SpanSummary; pid: string; tid: string }) {
  const q = useQuery({
    queryKey: ["span", pid, tid, summary.span_id],
    queryFn: () => api<Span>(projectPath(pid, `/traces/${tid}/spans/${summary.span_id}`)),
  });
  if (q.data) return <SpanPanel span={q.data} pid={pid} />;
  return (
    <div className="panel">
      <h2 style={{ margin: 0 }}>{summary.name}</h2>
      {q.error ? <div className="error-box">{String(q.error)}</div> : <div className="small muted">Loading span…</div>}
      {summary.error && <div className="error-box" style={{ marginTop: 8 }}>{summary.error}</div>}
      <h3>Input</h3>
      <pre className="json">{summary.input_preview ?? "—"}{summary.input_truncated ? " …" : ""}</pre>
      <h3>Output</h3>
      <pre className="json">{summary.output_preview ?? "—"}{summary.output_truncated ? " …" : ""}</pre>
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
