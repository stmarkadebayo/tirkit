import { Fragment, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, qs, type DatasetDetail, type DatasetSummary, type Example, type Json } from "../api";
import { usePid, useRole } from "../hooks";
import { fmtAgo, fmtNum, preview } from "../format";
import { Content, JsonBlock } from "../components/Content";

export function Datasets() {
  const pid = usePid();
  const nav = useNavigate();
  const role = useRole();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["datasets", pid], queryFn: () => api<DatasetSummary[]>(projectPath(pid, "/datasets")) });
  const [name, setName] = useState("");
  const [msg, setMsg] = useState("");
  const fromTraces = async () => {
    setMsg("");
    try {
      const r = await api<{ version: string; n_items: number }>(projectPath(pid, "/datasets/from-traces"), {
        method: "POST",
        json: { name, limit: 1000 },
      });
      setMsg(`${name}@${r.version}: ${r.n_items} examples`);
      void qc.invalidateQueries({ queryKey: ["datasets", pid] });
    } catch (e) {
      setMsg(String(e));
    }
  };
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Datasets</h1>
          <div className="muted small">Versioned test sets. Each change makes a new version; old versions stay readable.</div>
        </div>
        {role !== "viewer" && (
          <div className="row">
            <input placeholder="New dataset name" value={name} onChange={(e) => setName(e.target.value)} />
            <button className="primary" disabled={!name} onClick={fromTraces} title="Mine the latest 1,000 root traces, de-duplicated and PII-redacted">
              Build from recent traces
            </button>
          </div>
        )}
      </div>
      {msg && <div className="notice" style={{ marginBottom: 10 }}>{msg}</div>}
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Latest version</th>
              <th className="num">Items</th>
              <th className="num">Versions</th>
              <th>Created</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {q.data?.map((d) => (
              <tr key={d.name} className="clickable" onClick={() => nav(`/p/${pid}/datasets/${encodeURIComponent(d.name)}`)}>
                <td><b>{d.name}</b></td>
                <td className="mono">{d.latest_version}</td>
                <td className="num">{fmtNum(d.n_items)}</td>
                <td className="num">{d.n_versions}</td>
                <td className="small muted">{fmtAgo(d.created)}</td>
                <td className="small">{d.description}</td>
              </tr>
            ))}
            {q.data && !q.data.length && (
              <tr>
                <td colSpan={6} className="empty">
                  No datasets yet. Select traces on the Traces page and choose “Add to dataset”, build one from recent traces, or run <code>tirkit dataset import file.jsonl</code>.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function DatasetView() {
  const pid = usePid();
  const role = useRole();
  const qc = useQueryClient();
  const { name = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const version = params.get("version") || "";
  const q = useQuery({
    queryKey: ["dataset", pid, name, version],
    queryFn: () => api<DatasetDetail>(projectPath(pid, `/datasets/${encodeURIComponent(name)}${qs({ version })}`)),
  });
  const [open, setOpen] = useState<number | null>(null);
  const [editing, setEditing] = useState<{ idx: number; text: string } | null>(null);
  const [err, setErr] = useState("");
  const saveItems = async (items: Example[], note: string) => {
    setErr("");
    try {
      await api(projectPath(pid, `/datasets/${encodeURIComponent(name)}`), { method: "PUT", json: { items, note } });
      setEditing(null);
      setParams({}, { replace: true });
      void qc.invalidateQueries({ queryKey: ["dataset", pid, name] });
      void qc.invalidateQueries({ queryKey: ["datasets", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  const upload = async (file: File) => {
    const text = await file.text();
    await api(projectPath(pid, `/datasets/${encodeURIComponent(name)}/upload`), { method: "POST", body: text, headers: { "Content-Type": "application/x-ndjson" } });
    setParams({}, { replace: true });
    void qc.invalidateQueries({ queryKey: ["dataset", pid, name] });
  };
  const download = () => {
    if (!q.data) return;
    const blob = new Blob(q.data.items.map((e) => JSON.stringify(e) + "\n"), { type: "application/x-ndjson" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name}@${q.data.version}.jsonl`;
    a.click();
  };
  if (!q.data) return <div className="muted">{q.error ? String(q.error) : "Loading…"}</div>;
  const items = q.data.items;
  const canEdit = role !== "viewer";
  return (
    <div>
      <div className="small muted">
        <Link to={`/p/${pid}/datasets`}>Datasets</Link> / {name}
      </div>
      <div className="page-head">
        <div>
          <h1>{name}</h1>
          <div className="row small muted">
            <select value={q.data.version} onChange={(e) => setParams({ version: e.target.value })} aria-label="Version">
              {q.data.versions.map((v) => (
                <option key={v.version} value={v.version}>
                  {v.version} · {v.n_items} items · {fmtAgo(v.created)}
                  {v.note ? ` · ${v.note}` : ""}
                </option>
              ))}
            </select>
            <span>{fmtNum(items.length)} examples</span>
          </div>
        </div>
        <div className="row">
          <button onClick={download}>Download JSONL</button>
          {canEdit && (
            <label className="btn">
              Upload JSONL (new version)
              <input type="file" accept=".jsonl,.json,application/x-ndjson" hidden onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
            </label>
          )}
          <Link className="btn" to={`/p/${pid}/experiments?dataset=${encodeURIComponent(name)}`}>Experiments</Link>
        </div>
      </div>
      {err && <div className="error-box">{err}</div>}
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 40 }}>#</th>
              <th>Input</th>
              <th>Expected</th>
              <th>Source</th>
              {canEdit && <th />}
            </tr>
          </thead>
          <tbody>
            {items.map((e, i) => (
              <Fragment key={i}>
                <tr className="clickable" onClick={() => setOpen(open === i ? null : i)}>
                  <td className="muted">{i}</td>
                  <td className="truncate">{preview(e.input)}</td>
                  <td className="truncate">{preview(e.expected) || <span className="muted">—</span>}</td>
                  <td className="small">
                    {typeof e.metadata.source_trace_id === "string" && (
                      <Link to={`/p/${pid}/traces/${e.metadata.source_trace_id}`} onClick={(ev) => ev.stopPropagation()}>trace</Link>
                    )}
                  </td>
                  {canEdit && (
                    <td onClick={(ev) => ev.stopPropagation()}>
                      <button onClick={() => setEditing({ idx: i, text: JSON.stringify(e, null, 2) })}>Edit</button>{" "}
                      <button className="danger" onClick={() => saveItems(items.filter((_, j) => j !== i), `removed item ${i}`)}>Remove</button>
                    </td>
                  )}
                </tr>
                {open === i && (
                  <tr>
                    <td />
                    <td colSpan={canEdit ? 4 : 3}>
                      <div className="grid grid-2">
                        <div>
                          <h3>Input</h3>
                          <Content value={e.input as Json} pid={pid} />
                        </div>
                        <div>
                          <h3>Expected</h3>
                          <Content value={e.expected} pid={pid} />
                          {"reference_output" in e.metadata && (
                            <>
                              <h3 style={{ marginTop: 10 }}>Reference output (from the trace)</h3>
                              <Content value={e.metadata.reference_output} pid={pid} />
                            </>
                          )}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {canEdit && (
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={() => setEditing({ idx: items.length, text: JSON.stringify({ input: { question: "" }, expected: "", metadata: {} }, null, 2) })}>
            Add example
          </button>
        </div>
      )}
      {editing && (
        <div className="panel" style={{ marginTop: 12 }}>
          <h3>{editing.idx < items.length ? `Edit example ${editing.idx}` : "New example"}</h3>
          <textarea rows={12} value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} />
          <div className="row" style={{ marginTop: 8 }}>
            <button
              className="primary"
              onClick={() => {
                try {
                  const ex = JSON.parse(editing.text) as Example;
                  const next = [...items];
                  next[editing.idx] = { input: ex.input, expected: ex.expected ?? null, metadata: ex.metadata ?? {} };
                  void saveItems(next, editing.idx < items.length ? `edited item ${editing.idx}` : "added item");
                } catch (e) {
                  setErr(`Invalid JSON: ${e}`);
                }
              }}
            >
              Save as new version
            </button>
            <button onClick={() => setEditing(null)}>Cancel</button>
          </div>
          <JsonBlock value={"Shape: {\"input\": {...}, \"expected\": ..., \"metadata\": {...}}"} />
        </div>
      )}
    </div>
  );
}
