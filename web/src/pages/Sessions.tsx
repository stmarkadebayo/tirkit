import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, projectPath, type Trace } from "../api";
import { usePid } from "../hooks";
import { fmtAgo, fmtCost, fmtMs, fmtNum, preview } from "../format";

interface SessionRow {
  session_id: string;
  n_traces: number;
  start: number;
  end: number;
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  has_error: number;
  user_id: string | null;
}

export function Sessions() {
  const pid = usePid();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["sessions", pid], queryFn: () => api<SessionRow[]>(projectPath(pid, "/sessions?limit=200")) });
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Sessions</h1>
          <div className="muted small">Conversations and threads: traces grouped by session id.</div>
        </div>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Session</th>
              <th>Last activity</th>
              <th className="num">Traces</th>
              <th className="num">Duration</th>
              <th className="num">Tokens</th>
              <th className="num">Cost</th>
              <th>User</th>
            </tr>
          </thead>
          <tbody>
            {q.data?.map((s) => (
              <tr key={s.session_id} className="clickable" onClick={() => nav(`/p/${pid}/sessions/${encodeURIComponent(s.session_id)}`)}>
                <td className="mono">{s.has_error ? <span className="badge bad">error</span> : null} {s.session_id}</td>
                <td className="small muted">{fmtAgo(s.end || s.start)}</td>
                <td className="num">{s.n_traces}</td>
                <td className="num">{fmtMs(s.end && s.start ? (s.end - s.start) * 1000 : null)}</td>
                <td className="num">{fmtNum((s.input_tokens ?? 0) + (s.output_tokens ?? 0))}</td>
                <td className="num">{fmtCost(s.cost_usd)}</td>
                <td className="small">{s.user_id}</td>
              </tr>
            ))}
            {q.data && !q.data.length && (
              <tr>
                <td colSpan={7} className="empty">No sessions yet. Set <code>session_id</code> with <code>tirkit.trace_context()</code> or <code>gen_ai.conversation.id</code>.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SessionView() {
  const pid = usePid();
  const { sid = "" } = useParams();
  const q = useQuery({ queryKey: ["session", pid, sid], queryFn: () => api<{ traces: Trace[] }>(projectPath(pid, `/sessions/${encodeURIComponent(sid)}`)) });
  return (
    <div>
      <div className="small muted">
        <Link to={`/p/${pid}/sessions`}>Sessions</Link> / <span className="mono">{sid}</span>
      </div>
      <h1 style={{ marginBottom: 14 }}>Session</h1>
      <div className="grid" style={{ maxWidth: 900 }}>
        {q.data?.traces.map((t) => (
          <div key={t.trace_id} className="panel">
            <div className="row small muted" style={{ marginBottom: 6 }}>
              <Link to={`/p/${pid}/traces/${t.trace_id}`}>{t.name}</Link>
              <span>{new Date((t.start ?? 0) * 1000).toLocaleTimeString()}</span>
              <span>{fmtMs(t.duration_ms)}</span>
              <span>{fmtCost(t.cost_usd)}</span>
              {t.has_error && <span className="badge bad">error</span>}
            </div>
            <div className="msg user">
              <div className="role">user</div>
              <pre>{preview(t.input, 2000)}</pre>
            </div>
            <div className="msg assistant">
              <div className="role">assistant</div>
              <pre>{preview(t.output, 2000)}</pre>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
