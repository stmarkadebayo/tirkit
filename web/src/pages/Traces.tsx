import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, projectPath, qs, type Filters, type Trace } from "../api";
import { RANGES, usePid, useRole } from "../hooks";
import { fmtAgo, fmtCost, fmtMs, fmtNum, preview } from "../format";
import { AddToDataset } from "../components/AddToDataset";

const PAGE = 50;

export default function Traces() {
  const pid = usePid();
  const role = useRole();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const get = (k: string) => params.get(k) || "";
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    next.delete("page");
    setParams(next, { replace: true });
  };
  const page = Number(params.get("page") || 0);
  const range = get("range") || "7d";
  const [now] = useState(() => Math.floor(Date.now() / 1000));
  const filters = useQuery({ queryKey: ["filters", pid], queryFn: () => api<Filters>(projectPath(pid, "/filters")) });
  const query = {
    name: get("name"),
    user_id: get("user_id"),
    session_id: get("session_id"),
    release: get("release"),
    search: get("q"),
    error: get("error"),
    min_duration_ms: get("min_ms"),
    start_after: range === "all" ? "" : String(now - RANGES[range]),
    limit: PAGE,
    offset: page * PAGE,
  };
  const traces = useQuery({
    queryKey: ["traces", pid, query],
    queryFn: () => api<{ items: Trace[]; total: number }>(projectPath(pid, `/traces${qs(query)}`)),
    placeholderData: keepPreviousData,
  });
  const total = traces.data?.total ?? 0;
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Traces</h1>
          <div className="muted small">{fmtNum(total)} traces match</div>
        </div>
        <div className="row">
          {selected.size > 0 && role !== "viewer" && <AddToDataset traceIds={[...selected]} onDone={() => setSelected(new Set())} />}
          <a className="btn" href={projectPath(pid, `/export/traces.jsonl${qs({ ...query, limit: undefined, offset: undefined })}`)}>
            Export JSONL
          </a>
        </div>
      </div>
      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="row">
          <input placeholder="Search inputs and outputs" defaultValue={get("q")} onKeyDown={(e) => e.key === "Enter" && set("q", (e.target as HTMLInputElement).value)} style={{ flex: "1 1 220px" }} />
          <select value={get("name")} onChange={(e) => set("name", e.target.value)} aria-label="Trace name">
            <option value="">All names</option>
            {filters.data?.names.map((n) => <option key={n}>{n}</option>)}
          </select>
          <select value={get("release")} onChange={(e) => set("release", e.target.value)} aria-label="Release">
            <option value="">All releases</option>
            {filters.data?.releases.map((n) => <option key={n}>{n}</option>)}
          </select>
          <input placeholder="User id" defaultValue={get("user_id")} onKeyDown={(e) => e.key === "Enter" && set("user_id", (e.target as HTMLInputElement).value)} style={{ width: 120 }} />
          <input placeholder="Session id" defaultValue={get("session_id")} onKeyDown={(e) => e.key === "Enter" && set("session_id", (e.target as HTMLInputElement).value)} style={{ width: 130 }} />
          <select value={get("error")} onChange={(e) => set("error", e.target.value)} aria-label="Status">
            <option value="">Any status</option>
            <option value="true">Errors</option>
            <option value="false">OK</option>
          </select>
          <select value={get("min_ms")} onChange={(e) => set("min_ms", e.target.value)} aria-label="Minimum latency">
            <option value="">Any latency</option>
            <option value="1000">≥ 1 s</option>
            <option value="5000">≥ 5 s</option>
            <option value="20000">≥ 20 s</option>
          </select>
          <select value={range} onChange={(e) => set("range", e.target.value)} aria-label="Time range">
            {Object.keys(RANGES).map((r) => <option key={r} value={r}>last {r}</option>)}
            <option value="all">all time</option>
          </select>
        </div>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 28 }} />
                <th>Time</th>
                <th>Name</th>
                <th>Input</th>
                <th>Output</th>
                <th className="num">Latency</th>
                <th className="num">Tokens</th>
                <th className="num">Cost</th>
                <th>User</th>
              </tr>
            </thead>
            <tbody>
              {traces.data?.items.map((t) => (
                <tr key={t.trace_id} className="clickable" onClick={() => nav(`/p/${pid}/traces/${t.trace_id}`)}>
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" aria-label="select trace" checked={selected.has(t.trace_id)} onChange={() => toggle(t.trace_id)} />
                  </td>
                  <td className="small muted" title={new Date((t.start ?? 0) * 1000).toLocaleString()}>{fmtAgo(t.start)}</td>
                  <td>
                    {t.has_error && <span className="badge bad">error</span>} {t.name}
                  </td>
                  <td className="truncate">{preview(t.input)}</td>
                  <td className="truncate">{preview(t.output)}</td>
                  <td className="num">{fmtMs(t.duration_ms)}</td>
                  <td className="num">{fmtNum((t.input_tokens ?? 0) + (t.output_tokens ?? 0)) || "—"}</td>
                  <td className="num">{fmtCost(t.cost_usd)}</td>
                  <td className="small">{t.user_id}</td>
                </tr>
              ))}
              {traces.data && !traces.data.items.length && (
                <tr>
                  <td colSpan={9} className="empty">No traces match. Widen the time range or clear filters.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <span className="muted small">
          {total ? `${page * PAGE + 1}–${Math.min(total, (page + 1) * PAGE)} of ${fmtNum(total)}` : ""}
        </span>
        <div className="spacer" />
        <button disabled={page === 0} onClick={() => setParams((p) => { p.set("page", String(page - 1)); return p; })}>Previous</button>
        <button disabled={(page + 1) * PAGE >= total} onClick={() => setParams((p) => { p.set("page", String(page + 1)); return p; })}>Next</button>
      </div>
    </div>
  );
}
