import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type JudgeReport, type JudgeReports, type Queue } from "../api";
import { usePid, useRole } from "../hooks";
import { fmtAgo } from "../format";
import { CountBar, IntervalScale, KAPPA_BANDS, Metric, VerdictPill, fmt2 } from "../components/Reliability";
import {
  LABELS_NEEDED,
  flipVerdict,
  kappaVerdict,
  labelsProgress,
  lengthVerdict,
  nextAction,
  readReport,
  withInterval,
} from "../judgeReport";

export function JudgeReliability() {
  const pid = usePid();
  const role = useRole();
  const qc = useQueryClient();
  const { hash } = useLocation();
  const q = useQuery({
    queryKey: ["judge-reports", pid],
    queryFn: () => api<JudgeReports>(projectPath(pid, "/judge-reports")),
    refetchInterval: 10_000,
  });
  const queues = useQuery({ queryKey: ["queues", pid], queryFn: () => api<Queue[]>(projectPath(pid, "/queues")) });
  const [picked, setPicked] = useState<string | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  const [rubricOpen, setRubricOpen] = useState(false);
  const [f, setF] = useState({ queue: "", label: "", rubric: "correctness", text: "", model: "" });
  const [msg, setMsg] = useState("");
  useEffect(() => {
    if (hash === "#run" || hash === "#rubric") setRunOpen(true);
    if (hash === "#rubric") setRubricOpen(true);
  }, [hash]);

  const reports = q.data?.reports ?? [];
  const report: JudgeReport | undefined = reports.find((r) => r.report_id === picked) ?? reports[0];
  const reading = report ? readReport(report.summary) : null;
  const labels = (queues.data ?? []).reduce((n, x) => n + x.n_done, 0);
  const kv = reading ? kappaVerdict(reading) : kappaVerdict(readReport(null));
  const fv = reading ? flipVerdict(reading.flip, reading.nPairs) : flipVerdict(null, 0);
  const lv = reading ? lengthVerdict(reading.lengthJudge, reading.lengthHuman) : lengthVerdict(null, null);
  const flipCi = reading?.flipCi ?? null;
  const lp = labelsProgress(labels);
  const next = nextAction(labels, !!report, reading);

  const rubricText = f.text || q.data?.builtin[f.rubric] || "";
  const queue = queues.data?.find((x) => x.queue_id === f.queue);
  const start = async () => {
    setMsg("");
    try {
      await api(projectPath(pid, "/judge-reports"), {
        method: "POST",
        json: { queue_id: f.queue, label: f.label, rubric_name: f.rubric, rubric_text: rubricText, model: f.model },
      });
      setMsg("Running. The result appears at the top when the worker finishes.");
      void qc.invalidateQueries({ queryKey: ["judge-reports", pid] });
    } catch (e) {
      setMsg(String(e));
    }
  };

  return (
    <div style={{ maxWidth: 1000 }}>
      <div className="page-head">
        <div>
          <h1>Judge reliability</h1>
          <div className="muted small">
            An LLM judge is only useful if it agrees with people. This page compares the judge with your human labels, so you know how far to trust its scores.
          </div>
        </div>
      </div>

      <div className="grid">
        <section className="panel" aria-labelledby="agree-h">
          <h2 id="agree-h">Agreement with your labels</h2>
          {q.isLoading ? (
            <div className="muted">Loading…</div>
          ) : (
            <div className="headline">
              <div>
                {reading && reading.kappa !== null ? (
                  <>
                    <div className="big-number" aria-label={`Cohen's kappa ${reading.kappa.toFixed(2)}`}>{reading.kappa.toFixed(2)}</div>
                    <div className="small muted">Cohen's kappa</div>
                    <div className="small" style={{ marginTop: 4 }}>
                      {reading.ciLow !== null && reading.ciHigh !== null
                        ? `95% interval ${reading.ciLow.toFixed(2)} to ${reading.ciHigh.toFixed(2)}`
                        : "No interval was recorded for this report. Run it again to get one."}
                    </div>
                  </>
                ) : (
                  <div className="big-number muted">Not measured yet</div>
                )}
              </div>
              <div>
                <VerdictPill verdict={kv} />
                <p style={{ margin: "8px 0" }}>{kv.text}</p>
                {reading && reading.kappa !== null && (
                  <IntervalScale
                    min={-0.2}
                    max={1}
                    ticks={[0, 0.4, 0.6, 1]}
                    bands={KAPPA_BANDS}
                    low={reading.ciLow}
                    high={reading.ciHigh}
                    points={[{ value: reading.kappa, label: `kappa ${reading.kappa.toFixed(2)}` }]}
                    description={`Kappa ${reading.kappa.toFixed(2)} on a scale from -0.2 to 1; below 0.4 is poor, 0.4 to 0.6 fair, 0.6 and above good.`}
                  />
                )}
              </div>
            </div>
          )}
          <p className="small muted" style={{ margin: "10px 0 0" }}>
            What good looks like: kappa of 0.6 or more, with {LABELS_NEEDED} or more labels behind it. Kappa counts agreement beyond what chance gives: 0 is chance, 1 is perfect. These cut-offs are conventions, not guarantees.
          </p>
          {report && reading && (
            <p className="small muted" style={{ margin: "6px 0 0" }}>
              Based on {reading.nJudged} labelled items judged by <b>{report.model}</b> with rubric <b>{report.rubric_name}</b> <span className="mono">@{report.rubric_version}</span>, run {fmtAgo(report.created)}.
            </p>
          )}
        </section>

        <div className="grid grid-2">
          <section className="panel" aria-labelledby="pos-h">
            <h3 id="pos-h">Position bias</h3>
            <Metric
              label="Verdicts that flip when the answer order swaps"
              hint={
                reading && reading.nPairs
                  ? `${reading.nPairs} pairs with a clear winner${flipCi ? `; 95% interval ${Math.round(flipCi[0] * 100)}% to ${Math.round(flipCi[1] * 100)}%` : ""}`
                  : undefined
              }
            >
              {reading && reading.flip !== null ? `${Math.round(reading.flip * 100)}%` : <span className="muted small">not measured yet</span>}
            </Metric>
            {reading && reading.flip !== null && (
              <IntervalScale
                min={0}
                max={1}
                ticks={[0, 0.1, 0.25, 0.5, 1]}
                bands={[
                  { from: 0, to: 0.1, level: "good" },
                  { from: 0.1, to: 0.25, level: "fair" },
                  { from: 0.25, to: 1, level: "poor" },
                ]}
                low={flipCi?.[0]}
                high={flipCi?.[1]}
                points={[{ value: reading.flip, label: `flip rate ${(reading.flip * 100).toFixed(0)}%` }]}
                description={`Flip rate ${(reading.flip * 100).toFixed(0)} percent on a scale from 0 to 100 percent; lower is better.`}
              />
            )}
            <div style={{ margin: "6px 0" }}><VerdictPill verdict={fv} /></div>
            <p className="small muted" style={{ margin: 0 }}>{fv.text} Good: under 10%, interval shown is 95%.</p>
          </section>

          <section className="panel" aria-labelledby="len-h">
            <h3 id="len-h">Length bias</h3>
            <Metric
              label="How much more the judge's score follows answer length than yours (-2 to 2)"
              hint={
                reading && reading.lengthJudge !== null && reading.lengthHuman !== null
                  ? `judge ${fmt2(reading.lengthJudge)}, humans ${fmt2(reading.lengthHuman)}; ${
                      reading.lengthBiasCi
                        ? `95% interval ${reading.lengthBiasCi[0].toFixed(2)} to ${reading.lengthBiasCi[1].toFixed(2)}`
                        : "no interval recorded for this report"
                    }`
                  : undefined
              }
            >
              {reading && reading.lengthBias !== null ? reading.lengthBias.toFixed(2) : <span className="muted small">not measured yet</span>}
            </Metric>
            {reading && reading.lengthJudge !== null && reading.lengthHuman !== null && (
              <>
                <IntervalScale
                  min={-1}
                  max={1}
                  ticks={[-1, 0, 1]}
                  points={[
                    { value: reading.lengthHuman, label: `humans ${reading.lengthHuman.toFixed(2)}`, kind: "other" },
                    { value: reading.lengthJudge, label: `judge ${reading.lengthJudge.toFixed(2)}` },
                  ]}
                  description={`Correlation of score with answer length: judge ${reading.lengthJudge.toFixed(2)}, humans ${reading.lengthHuman.toFixed(2)}, on a scale from -1 to 1.`}
                />
                <div className="legend"><span><i />judge</span><span><i className="other" />your labels</span></div>
              </>
            )}
            <div style={{ margin: "6px 0" }}><VerdictPill verdict={lv} /></div>
            <p className="small muted" style={{ margin: 0 }}>{lv.text} Good: the two dots sit close together. The number above is judge minus humans.</p>
          </section>
        </div>

        <div className="grid grid-2">
          <section className="panel" aria-labelledby="lab-h">
            <h3 id="lab-h">Labels</h3>
            <CountBar done={lp.done} needed={lp.needed} text={lp.text} />
            <p className="small muted" style={{ margin: "8px 0 0" }}>The judge is checked against these. Twenty give a first reading; about {LABELS_NEEDED} give a firm one.</p>
          </section>
          <section className="panel" aria-labelledby="next-h">
            <h3 id="next-h">What to do next</h3>
            <p style={{ margin: "0 0 10px" }}>{next.text}</p>
            <Link className="btn primary" to={`/p/${pid}/${next.to}`}>{next.cta}</Link>
          </section>
        </div>

        {reading && report && (
          <details className="panel fold">
            <summary>More numbers for this report</summary>
            <dl className="kv" style={{ marginTop: 8 }}>
              <dt>Exact agreement</dt><dd>{withInterval(reading.exact, reading.exactCi)}</dd>
              <dt>Weighted kappa</dt><dd>{withInterval(reading.weighted, reading.weightedCi)}</dd>
              <dt>Pass/fail kappa</dt><dd>{withInterval(reading.passFail, reading.passFailCi)}</dd>
              <dt>Items labelled / judged</dt><dd>{reading.nLabelled} / {reading.nJudged}</dd>
            </dl>
          </details>
        )}

        {reports.length > 1 && (
          <section className="panel" style={{ padding: 0 }}>
            <h3 style={{ padding: "12px 16px 0" }}>Earlier reports</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>When</th><th>Rubric</th><th>Judge model</th><th className="num">Labels</th><th className="num">κ</th><th className="num">95% interval</th><th /></tr>
                </thead>
                <tbody>
                  {reports.map((r) => {
                    const rr = readReport(r.summary);
                    return (
                      <tr key={r.report_id} className={r.report_id === report?.report_id ? "active-row" : undefined}>
                        <td className="small muted">{fmtAgo(r.created)}</td>
                        <td>{r.rubric_name} <span className="mono small muted">@{r.rubric_version}</span></td>
                        <td className="small">{r.model}</td>
                        <td className="num">{rr.nJudged}</td>
                        <td className="num">{fmt2(rr.kappa)}</td>
                        <td className="num small muted">{rr.ciLow !== null && rr.ciHigh !== null ? `${rr.ciLow.toFixed(2)} to ${rr.ciHigh.toFixed(2)}` : "not recorded"}</td>
                        <td><button onClick={() => setPicked(r.report_id)} disabled={r.report_id === report?.report_id}>Show</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {role !== "viewer" && (
          <details className="panel fold" id="run" open={runOpen} onToggle={(e) => setRunOpen((e.target as HTMLDetailsElement).open)}>
            <summary>Run a judge against your labels</summary>
            <div style={{ marginTop: 10 }}>
              <div className="row" style={{ marginBottom: 8 }}>
                <select value={f.queue} onChange={(e) => setF({ ...f, queue: e.target.value, label: "" })} aria-label="Queue">
                  <option value="">labels from queue…</option>
                  {queues.data?.map((x) => <option key={x.queue_id} value={x.queue_id}>{x.name} ({x.n_done} labelled)</option>)}
                </select>
                <select value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} aria-label="Human label">
                  <option value="">human score…</option>
                  {queue?.score_names.map((s) => <option key={s}>{s}</option>)}
                </select>
                <select value={f.rubric} onChange={(e) => setF({ ...f, rubric: e.target.value, text: "" })} aria-label="Rubric">
                  {Object.keys(q.data?.builtin ?? {}).map((r) => <option key={r}>{r}</option>)}
                </select>
                <input placeholder="judge model" aria-label="Judge model" value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} />
                <button className="primary" disabled={!f.queue || !f.label || !f.model} onClick={start}>Run report</button>
              </div>
              <details id="rubric" className="fold" open={rubricOpen} onToggle={(e) => setRubricOpen((e.target as HTMLDetailsElement).open)}>
                <summary>Rubric text (edit to calibrate; a changed text is a new rubric version)</summary>
                <textarea rows={7} aria-label="Rubric text" value={rubricText} onChange={(e) => setF({ ...f, text: e.target.value })} style={{ fontFamily: "inherit", marginTop: 8 }} />
              </details>
              {msg && <div className="small muted" style={{ marginTop: 6 }}>{msg}</div>}
              <div className="small muted mono" style={{ marginTop: 8 }}>CLI: tirkit judge-report labels.jsonl --label correct --rubric correctness --model &lt;judge&gt;</div>
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
