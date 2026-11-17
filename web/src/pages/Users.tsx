import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, projectPath, type UserRow } from "../api";
import { usePid } from "../hooks";
import { fmtAgo, fmtCost, fmtNum } from "../format";

export default function Users() {
  const pid = usePid();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["users", pid], queryFn: () => api<UserRow[]>(projectPath(pid, "/users?limit=500")) });
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Users</h1>
          <div className="muted small">End users of your app, from the <code>user_id</code> on traces.</div>
        </div>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>First seen</th>
              <th>Last seen</th>
              <th className="num">Traces</th>
              <th className="num">Errors</th>
              <th className="num">Tokens</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {q.data?.map((u) => (
              <tr key={u.user_id} className="clickable" onClick={() => nav(`/p/${pid}/traces?user_id=${encodeURIComponent(u.user_id)}&range=all`)}>
                <td>{u.user_id}</td>
                <td className="small muted">{fmtAgo(u.first_seen)}</td>
                <td className="small muted">{fmtAgo(u.last_seen)}</td>
                <td className="num">{fmtNum(u.n_traces)}</td>
                <td className="num">{fmtNum(u.n_errors)}</td>
                <td className="num">{fmtNum((u.input_tokens ?? 0) + (u.output_tokens ?? 0))}</td>
                <td className="num">{fmtCost(u.cost_usd)}</td>
              </tr>
            ))}
            {q.data && !q.data.length && (
              <tr>
                <td colSpan={7} className="empty">No users yet. Set <code>user_id</code> with <code>tirkit.trace_context()</code>.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
