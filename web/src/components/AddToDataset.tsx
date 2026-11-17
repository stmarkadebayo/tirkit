import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type DatasetSummary } from "../api";
import { usePid } from "../hooks";

/** Adds selected traces (redacted, de-duplicated) to a new or existing dataset. */
export function AddToDataset({ traceIds, onDone }: { traceIds: string[]; onDone?: () => void }) {
  const pid = usePid();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [asExpected, setAsExpected] = useState(false);
  const [msg, setMsg] = useState("");
  const datasets = useQuery({
    queryKey: ["datasets", pid],
    queryFn: () => api<DatasetSummary[]>(projectPath(pid, "/datasets")),
    enabled: open,
  });
  const save = async () => {
    try {
      const r = await api<{ name: string; version: string; n_items: number }>(projectPath(pid, "/datasets/from-traces"), {
        method: "POST",
        json: { name, trace_ids: traceIds, append: true, expected_from_output: asExpected },
      });
      setMsg(`Saved ${r.name}@${r.version} (${r.n_items} items)`);
      void qc.invalidateQueries({ queryKey: ["datasets", pid] });
      onDone?.();
    } catch (e) {
      setMsg(String(e));
    }
  };
  if (!open) return <button onClick={() => setOpen(true)}>Add {traceIds.length} to dataset</button>;
  return (
    <div className="row panel" style={{ padding: 8 }}>
      <input list="dataset-names" placeholder="Dataset name" value={name} onChange={(e) => setName(e.target.value)} />
      <datalist id="dataset-names">
        {datasets.data?.map((d) => <option key={d.name} value={d.name} />)}
      </datalist>
      <label className="small">
        <input type="checkbox" checked={asExpected} onChange={(e) => setAsExpected(e.target.checked)} /> outputs as expected
      </label>
      <button className="primary" disabled={!name} onClick={save}>Save</button>
      <button onClick={() => setOpen(false)}>Cancel</button>
      {msg && <span className="small muted">{msg}</span>}
    </div>
  );
}
