import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, projectPath, qs, type CompareResult, type ExperimentSummary, type JudgeReports, type Metrics, type Queue } from "../api";
import { usePid } from "../hooks";
import { fmtAgo, fmtCost, fmtMs, fmtNum } from "../format";
import { SetupChecklist, useProgress } from "../components/SetupChecklist";
import { CountBar, IntervalScale, KAPPA_BANDS, VerdictPill } from "../components/Reliability";
import { Stat } from "../components/Stat";
import { kappaVerdict, labelsProgress, nextAction, readReport } from "../judgeReport";
import { gateFromComparisons, latestPair } from "../overview";

/** The landing page after sign-in: can you trust your evaluation, then what changed, then traffic. */
export default function Overview() {
  const pid = usePid();
  const { ready, progress } = useProgress(pid);
  const newProject = ready && progress.traceCount === 0;
  return (
    <div style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <div className="muted small">Whether your evaluation can be trusted, how your changes are doing, and your traffic.</div>
        </div>
      </div>
      {newProject && <SetupChecklist pid={pid} />}
      <div className="grid">
        <ReliabilityBlock pid={pid} />
        <ExperimentsBlock pid={pid} />
        <TrafficBlock pid={pid} />
        {!newProject && <SetupChecklist pid={pid} />}
      </div>
    </div>
  );
}

function ReliabilityBlock({ pid }: { pid: string }) {
  const reports = useQuery({ queryKey: ["judge-reports", pid], queryFn: () => api<JudgeReports>(projectPath(pid, "/judge-reports")) });
  const queues = useQuery({ queryKey: ["queues", pid], queryFn: () => api<Queue[]>(projectPath(pid, "/queues")) });
  const labels = (queues.data ?? []).reduce((n, q) => n + q.n_done, 0);
  const report = reports.data?.reports[0];
  const reading = report ? readReport(report.summary) : null;
  const verdict = kappaVerdict(reading ?? readReport(null));
  const lp = labelsProgress(labels);
  const next = nextAction(labels, !!report, reading);
  const open = queues.data?.find((q) => q.n_done < q.n_items);
  const labelTarget = open ? `annotate/${open.queue_id}` : "annotation";
  const loading = reports.isLoading || queues.isLoading;
  const measured = !!reading && reading.kappa !== null;
  return (
    <section className="panel" aria-labelledby="rel-h">
      <div className="row">
        <h2 id="rel-h" style={{ margin: 0 }}>Can you trust your judge?</h2>
        <div className="spacer" />
        <Link to={`/p/${pid}/judge`}>Details</Link>
      </div>
      {loading ? (
        <div className="muted" style={{ marginTop: 10 }}>Loading…</div>
      ) : measured && reading ? (
        <div className="headline" style={{ marginTop: 12 }}>
          <div>
            <div className="big-number">{reading.kappa!.toFixed(2)}</div>
            <div className="small muted">agreement with human labels (kappa)</div>
            <div className="small">
              {reading.ciLow !== null && reading.ciHigh !== null ? `95% interval ${reading.ciLow.toFixed(2)} to ${reading.ciHigh.toFixed(2)}` : "interval not recorded for this report"}
            </div>
          </div>
          <div>
            <VerdictPill verdict={verdict} />
            <p style={{ margin: "8px 0" }}>{verdict.text}</p>
            <IntervalScale
              min={-0.2}
              max={1}
              ticks={[0, 0.4, 0.6, 1]}
              bands={KAPPA_BANDS}
              low={reading.ciLow}
              high={reading.ciHigh}
              points={[{ value: reading.kappa, label: `kappa ${reading.kappa!.toFixed(2)}` }]}
              description={`Kappa ${reading.kappa!.toFixed(2)} on a scale from -0.2 to 1.`}
            />
            <div className="small muted">Last measured {fmtAgo(report!.created)} on {reading.nJudged} labels.</div>
          </div>
        </div>
      ) : (
        <div className="headline" style={{ marginTop: 12 }}>
          <div>
            <div className="big-number muted">Not measured yet</div>
            <div className="small muted">agreement with human labels</div>
          </div>
          <div>
            <CountBar done={lp.done} needed={lp.needed} text={`${lp.text} (about 100 are needed for a firm reading; 20 give a first one)`} />
            <p style={{ margin: "10px 0" }}>{next.text}</p>
            <Link className="btn primary" to={`/p/${pid}/${labelTarget}`}>Open the annotation queue</Link>
          </div>
        </div>
      )}
    </section>
  );
}

function ExperimentsBlock({ pid }: { pid: string }) {
  const exps = useQuery({ queryKey: ["experiments", pid, ""], queryFn: () => api<ExperimentSummary[]>(projectPath(pid, "/experiments")) });
  const pair = useMemo(() => (exps.data ? latestPair(exps.data) : null), [exps.data]);
  const cmp = useQuery({
    queryKey: ["compare", pid, pair?.baseline?.experiment_id, pair?.candidate.experiment_id],
    queryFn: () => api<CompareResult>(projectPath(pid, `/compare${qs({ baseline: pair!.baseline!.experiment_id, candidate: pair!.candidate.experiment_id })}`)),
    enabled: !!pair?.baseline,
  });
  const gate = gateFromComparisons(pair?.baseline ? cmp.data?.comparisons : null);
  const recent = (exps.data ?? []).slice(0, 3);
  return (
    <section className="panel" aria-labelledby="exp-h">
      <div className="row">
        <h2 id="exp-h" style={{ margin: 0 }}>Experiments and the CI gate</h2>
        <div className="spacer" />
        <Link to={`/p/${pid}/experiments`}>All experiments</Link>
      </div>
      {exps.isLoading ? (
        <div className="muted" style={{ marginTop: 10 }}>Loading…</div>
      ) : !recent.length ? (
        <p style={{ margin: "10px 0 0" }} className="muted">
          No experiments yet. <Link to={`/p/${pid}/experiments`}>See how to run one</Link>.
        </p>
      ) : (
        <>
          <div className="row" style={{ margin: "10px 0 6px" }}>
            <span className={`verdict ${gate.state === "pass" ? "good" : gate.state === "fail" ? "poor" : "unmeasured"}`}>
              <span aria-hidden>{gate.state === "pass" ? "✓" : gate.state === "fail" ? "✕" : "–"}</span> {gate.title}
            </span>
            {pair?.baseline && cmp.data && (
              <Link to={`/p/${pid}/compare?baseline=${pair.baseline.experiment_id}&candidate=${pair.candidate.experiment_id}`}>See the comparison</Link>
            )}
          </div>
          <p className="small muted" style={{ margin: "0 0 10px" }}>
            {gate.state === "none" && pair && !pair.baseline ? "Run the newest experiment's dataset version again to get something to compare with." : gate.text} Same rule as <code>tirkit gate</code>: fail only if confidently worse.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Run</th><th>Dataset</th><th>When</th></tr>
              </thead>
              <tbody>
                {recent.map((e) => (
                  <tr key={e.experiment_id}>
                    <td><Link to={`/p/${pid}/experiments/${e.experiment_id}`}>{e.name || e.experiment_id}</Link></td>
                    <td className="small">{e.dataset}</td>
                    <td className="small muted">{fmtAgo(e.created)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function TrafficBlock({ pid }: { pid: string }) {
  const now = useMemo(() => Math.floor(Date.now() / 1000), []);
  const m = useQuery({
    queryKey: ["metrics", pid, "overview"],
    queryFn: () => api<Metrics>(projectPath(pid, `/metrics${qs({ since: now - 86400, until: now, buckets: 24 })}`)),
  });
  const t = useMemo(() => {
    const pts = m.data?.series.all ?? [];
    let traces = 0, errors = 0, cost = 0, p95: number | null = null;
    for (const p of pts) {
      traces += p.traces ?? 0;
      errors += p.trace_errors ?? 0;
      cost += p.cost_usd ?? 0;
      if (p.p95_ms !== undefined && p.p95_ms !== null) p95 = Math.max(p95 ?? 0, p.p95_ms);
    }
    return { traces, errors, cost, p95 };
  }, [m.data]);
  return (
    <section aria-labelledby="traffic-h">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 id="traffic-h" style={{ margin: 0 }}>Traffic, last 24 hours</h2>
        <div className="spacer" />
        <Link to={`/p/${pid}/dashboard`}>Open the dashboard</Link>
      </div>
      <div className="grid grid-4">
        <Stat label="Traces" value={fmtNum(t.traces)} />
        <Stat label="Error rate" value={t.traces ? `${((100 * t.errors) / t.traces).toFixed(1)}%` : "—"} sub={`${fmtNum(t.errors)} failed traces`} />
        <Stat label="Cost" value={fmtCost(t.cost)} />
        <Stat label="Worst p95 latency" value={fmtMs(t.p95)} />
      </div>
    </section>
  );
}
