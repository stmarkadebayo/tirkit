import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type ApiKeyInfo, type DatasetSummary, type ExperimentSummary, type JudgeReports, type Queue, type Trace } from "../api";
import { useRole } from "../hooks";
import {
  SNIPPET_TABS,
  allDone,
  buildChecklist,
  isDismissed,
  nextStep,
  secretNote,
  setDismissed,
  snippet,
  stepsDone,
  type Progress,
  type SnippetTab,
} from "../onboarding";
import { CodeBlock } from "./CodeBlock";

/** What the checklist needs, from the real API. Shares query keys with the pages it links to. */
export function useProgress(pid: string) {
  const role = useRole();
  const keys = useQuery({
    queryKey: ["keys", pid],
    queryFn: () => api<ApiKeyInfo[]>(projectPath(pid, "/keys")),
    enabled: role === "admin",
  });
  // Polls every 2.5 s only while the project has no trace; react-query pauses it in a hidden tab.
  const probe = useQuery({
    queryKey: ["trace-probe", pid],
    queryFn: () => api<{ items: Trace[]; total: number }>(projectPath(pid, "/traces?limit=1")),
    refetchInterval: (q) => (q.state.data && q.state.data.total > 0 ? false : 2500),
    refetchIntervalInBackground: false,
  });
  const datasets = useQuery({ queryKey: ["datasets", pid], queryFn: () => api<DatasetSummary[]>(projectPath(pid, "/datasets")) });
  const experiments = useQuery({ queryKey: ["experiments", pid, ""], queryFn: () => api<ExperimentSummary[]>(projectPath(pid, "/experiments")) });
  const queues = useQuery({ queryKey: ["queues", pid], queryFn: () => api<Queue[]>(projectPath(pid, "/queues")) });
  const reports = useQuery({ queryKey: ["judge-reports", pid], queryFn: () => api<JudgeReports>(projectPath(pid, "/judge-reports")) });
  const ready = [probe, datasets, experiments, queues, reports].every((q) => q.data !== undefined);
  const progress: Progress = {
    hasKey: role === "admin" ? (keys.data ? keys.data.some((k) => !k.expired) : null) : null,
    traceCount: probe.data?.total ?? 0,
    datasets: datasets.data?.length ?? 0,
    experiments: experiments.data?.length ?? 0,
    labels: (queues.data ?? []).reduce((n, q) => n + q.n_done, 0),
    judgeReports: reports.data?.reports.length ?? 0,
  };
  const publicKey = keys.data?.find((k) => !k.expired)?.public_key ?? null;
  return { ready, progress, publicKey, firstTraceId: probe.data?.items[0]?.trace_id ?? null };
}

export function SetupChecklist({ pid }: { pid: string }) {
  const { ready, progress, publicKey, firstTraceId } = useProgress(pid);
  const [dismissed, setDismissedState] = useState(() => isDismissed(pid));
  const [startedEmpty, setStartedEmpty] = useState(false);
  const role = useRole();
  const qc = useQueryClient();
  const [created, setCreated] = useState<{ public_key: string; secret_key: string } | null>(null);
  const [keyErr, setKeyErr] = useState("");
  const createKey = async () => {
    setKeyErr("");
    try {
      setCreated(await api<{ public_key: string; secret_key: string }>(projectPath(pid, "/keys"), { method: "POST", json: { name: "first trace" } }));
      void qc.invalidateQueries({ queryKey: ["keys", pid] });
    } catch (e) {
      setKeyErr(String(e));
    }
  };
  useEffect(() => setDismissedState(isDismissed(pid)), [pid]);
  useEffect(() => {
    if (ready && progress.traceCount === 0) setStartedEmpty(true);
  }, [ready, progress.traceCount]);
  if (!ready) return null;

  const steps = buildChecklist(progress);
  const done = stepsDone(steps);
  if (allDone(steps) && !startedEmpty) return null;
  const dismiss = (on: boolean) => {
    setDismissed(pid, on);
    setDismissedState(on);
  };
  if (dismissed) {
    return (
      <div className="small muted" style={{ marginBottom: 12 }}>
        <button className="linklike" onClick={() => dismiss(false)}>Show the setup checklist ({done} of {steps.length} done)</button>
      </div>
    );
  }
  const next = nextStep(steps);
  const showTraceGuide = progress.traceCount === 0 || startedEmpty;
  return (
    <section className="panel checklist" aria-labelledby="checklist-title" style={{ marginBottom: 14 }}>
      <div className="row">
        <div>
          <h2 id="checklist-title" style={{ marginBottom: 2 }}>{progress.traceCount === 0 ? "Send your first trace" : "Finish setting up"}</h2>
          <div className="small muted">{done} of {steps.length} steps done</div>
        </div>
        <div className="spacer" />
        <button onClick={() => dismiss(true)}>Dismiss</button>
      </div>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={steps.length} aria-valuenow={done} aria-label="Setup progress">
        <div style={{ width: `${(100 * done) / steps.length}%` }} />
      </div>
      <ol className="steps">
        {steps.map((s, i) => (
          <li key={s.id} className={`step${s.done ? " done" : ""}${next?.id === s.id ? " next" : ""}`}>
            <span className="step-mark" aria-hidden>{s.done ? "✓" : i + 1}</span>
            <div className="step-body">
              <div className="step-title">
                {s.title} {s.done && <span className="badge good">done</span>}
              </div>
              {!s.done && <div className="small muted">{s.detail}</div>}
              {s.id === "key" && keyErr && <div className="error-box">{keyErr}</div>}
              {s.id === "trace" && showTraceGuide && (
                <FirstTrace pid={pid} publicKey={created?.public_key ?? publicKey} secretKey={created?.secret_key ?? null} gotIt={progress.traceCount > 0} firstTraceId={firstTraceId} />
              )}
            </div>
            {!s.done && s.id === "key" && role === "admin" && <button className="primary" onClick={createKey}>{s.cta}</button>}
            {!s.done && s.id !== "trace" && s.id !== "key" && <Link className="btn" to={`/p/${pid}/${s.to}`}>{s.cta}</Link>}
          </li>
        ))}
      </ol>
    </section>
  );
}

function FirstTrace({ pid, publicKey, secretKey, gotIt, firstTraceId }: { pid: string; publicKey: string | null; secretKey: string | null; gotIt: boolean; firstTraceId: string | null }) {
  const [tab, setTab] = useState<SnippetTab>("python");
  const input = { host: window.location.origin, publicKey, secretKey };
  return (
    <div className="first-trace">
      <div className="listening" role="status" aria-live="polite">
        {gotIt ? (
          <>
            <span className="badge good">Got it</span> Your first trace arrived.{" "}
            {firstTraceId && <Link to={`/p/${pid}/traces/${firstTraceId}`}>Open the trace</Link>}
          </>
        ) : (
          <>
            <span className="pulse" aria-hidden /> Listening for your first trace…
          </>
        )}
      </div>
      {!gotIt && (
        <>
          <div className="tabs" role="tablist" aria-label="Language" style={{ marginTop: 10 }}>
            {SNIPPET_TABS.map((t) => (
              <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </div>
          <CodeBlock code={snippet(tab, input)} />
          <div className={`small ${secretKey ? "notice" : "muted"}`} style={{ marginTop: 6 }}>{secretNote(input)}</div>
        </>
      )}
    </div>
  );
}
