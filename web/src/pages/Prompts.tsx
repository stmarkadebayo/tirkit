import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type Json, type PromptSummary, type PromptVersion, type PromptVersions } from "../api";

export type { PromptVersion };
import { usePid, useRole } from "../hooks";
import { fmtAgo } from "../format";
import { JsonBlock } from "../components/Content";

export function Prompts() {
  const pid = usePid();
  const nav = useNavigate();
  const q = useQuery({
    queryKey: ["prompts", pid],
    queryFn: () => api<PromptSummary[]>(projectPath(pid, "/prompts")),
  });
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Prompts</h1>
          <div className="muted small">
            Versioned prompts. Apps fetch them by label with <code>tirkit.get_prompt("name")</code>; moving the <code>production</code> label deploys a version without a code change.
          </div>
        </div>
        <Link className="btn" to={`/p/${pid}/playground`}>New prompt in playground</Link>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th className="num">Versions</th>
              <th>Labels</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {q.data?.map((p) => (
              <tr key={p.name} className="clickable" onClick={() => nav(`/p/${pid}/prompts/${encodeURIComponent(p.name)}`)}>
                <td><b>{p.name}</b></td>
                <td className="num">{p.latest_version}</td>
                <td>
                  {Object.entries(p.labels).map(([l, v]) => (
                    <span key={l} className={`badge ${l === "production" ? "good" : ""}`} style={{ marginRight: 4 }}>
                      {l} → v{v}
                    </span>
                  ))}
                </td>
                <td className="small muted">{fmtAgo(p.created)}</td>
              </tr>
            ))}
            {q.data && !q.data.length && (
              <tr>
                <td colSpan={4} className="empty">No prompts yet. Write one in the playground and save it.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function PromptView() {
  const pid = usePid();
  const role = useRole();
  const nav = useNavigate();
  const qc = useQueryClient();
  const { name = "" } = useParams();
  const q = useQuery({
    queryKey: ["prompt", pid, name],
    queryFn: () => api<PromptVersions>(projectPath(pid, `/prompts/${encodeURIComponent(name)}/versions`)),
  });
  const [sel, setSel] = useState<number | null>(null);
  const [label, setLabel] = useState("production");
  const [err, setErr] = useState("");
  if (!q.data) return <div className="muted">{q.error ? String(q.error) : "Loading…"}</div>;
  const v = q.data.versions.find((x) => x.version === sel) ?? q.data.versions[0];
  const setLabelTo = async (lab: string, version: number) => {
    setErr("");
    try {
      await api(projectPath(pid, `/prompts/${encodeURIComponent(name)}/labels/${lab}`), { method: "PUT", json: { version } });
      void qc.invalidateQueries({ queryKey: ["prompt", pid, name] });
      void qc.invalidateQueries({ queryKey: ["prompts", pid] });
    } catch (e) {
      setErr(String(e));
    }
  };
  const openInPlayground = () => {
    sessionStorage.setItem(
      "tirkit.playground",
      JSON.stringify({ kind: v.kind, template: v.template, model: v.config.model ?? "", params: v.config.params ?? {}, promptName: name, promptVersion: v.version }),
    );
    nav(`/p/${pid}/playground`);
  };
  return (
    <div>
      <div className="small muted">
        <Link to={`/p/${pid}/prompts`}>Prompts</Link> / {name}
      </div>
      <h1 style={{ marginBottom: 12 }}>{name}</h1>
      {err && <div className="error-box">{err}</div>}
      <div className="split">
        <div className="panel" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Labels</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {q.data.versions.map((x) => (
                <tr key={x.version} className="clickable" onClick={() => setSel(x.version)} style={x.version === v.version ? { background: "var(--accent-soft)" } : undefined}>
                  <td>
                    <b>v{x.version}</b>
                    <div className="small muted">{fmtAgo(x.created)} · {x.created_by}</div>
                  </td>
                  <td>
                    {x.labels.map((l) => (
                      <span key={l} className={`badge ${l === "production" ? "good" : ""}`} style={{ marginRight: 4 }}>{l}</span>
                    ))}
                  </td>
                  <td className="small">{x.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel">
          <div className="row" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>v{v.version}</h2>
            <span className="badge">{v.kind}</span>
            {v.variables.map((x) => <span key={x} className="badge accent">{`{{${x}}}`}</span>)}
            <div className="spacer" />
            <button onClick={openInPlayground}>Open in playground</button>
          </div>
          {role !== "viewer" && (
            <div className="row" style={{ marginBottom: 10 }}>
              <input aria-label="Label" value={label} onChange={(e) => setLabel(e.target.value)} style={{ width: 140 }} />
              <button className="primary" onClick={() => setLabelTo(label, v.version)}>Point label at v{v.version}</button>
            </div>
          )}
          {v.kind === "chat" && Array.isArray(v.template) ? (
            (v.template as { role: string; content: Json }[]).map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                <div className="role">{m.role}</div>
                <pre>{typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 2)}</pre>
              </div>
            ))
          ) : (
            <pre className="json">{String(v.template)}</pre>
          )}
          {Object.keys(v.config).length > 0 && (
            <>
              <h3 style={{ marginTop: 10 }}>Model config</h3>
              <JsonBlock value={v.config} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
