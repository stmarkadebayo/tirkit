import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api, projectPath, qs, type AuditPage } from "../api";
import { usePid, useRole } from "../hooks";

const PREFIXES = [
  { value: "", label: "All events" },
  { value: "key.", label: "API keys" },
  { value: "member.", label: "Members" },
  { value: "settings.", label: "Settings" },
  { value: "export.", label: "Exports" },
];

/** Append-only record of who did what: sign-ins, keys, members, settings and exports. */
export default function Audit() {
  const pid = usePid();
  const role = useRole();
  const [action, setAction] = useState("");
  const q = useInfiniteQuery({
    queryKey: ["audit", pid, action],
    enabled: role === "admin",
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => api<AuditPage>(projectPath(pid, `/audit${qs({ action, before: pageParam, limit: 50 })}`)),
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  if (role !== "admin") return <div className="notice">The audit log is visible to project admins.</div>;
  const rows = q.data?.pages.flatMap((p) => p.entries) ?? [];
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Audit log</h1>
          <div className="muted small">Append-only: entries cannot be edited or deleted from the app.</div>
        </div>
        <select value={action} onChange={(e) => setAction(e.target.value)} aria-label="Filter events">
          {PREFIXES.map((p) => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>
      </div>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>Action</th>
              <th>Target</th>
              <th>Address</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.id}>
                <td className="small muted">{new Date(e.at * 1000).toLocaleString()}</td>
                <td>{e.actor ?? "anonymous"}</td>
                <td className="mono">{e.action}</td>
                <td className="small mono">{e.target}</td>
                <td className="small mono">{e.ip}</td>
                <td className="small mono">{Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}</td>
              </tr>
            ))}
            {!rows.length && !q.isLoading && (
              <tr>
                <td colSpan={6} className="muted">No events yet.</td>
              </tr>
            )}
          </tbody>
        </table>
        {q.hasNextPage && (
          <button style={{ marginTop: 10 }} disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
            Load older
          </button>
        )}
      </div>
    </div>
  );
}
