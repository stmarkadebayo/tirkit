import { Fragment, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, projectPath, qs, type CompareResult, type ExperimentDetail, type ExperimentSummary, type Json } from "../api";
import { usePid } from "../hooks";
import { fmtAgo, fmtMs, preview } from "../format";
import { CIBar } from "../components/Charts";
import { Content } from "../components/Content";
import { CodeBlock } from "../components/CodeBlock";
import { EmptyState } from "../components/EmptyState";

const ci = (c?: [number, number]) => (c ? `[${c[0].toFixed(3)}, ${c[1].toFixed(3)}]` : "");

export function Experiments() {
  const pid = usePid();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const dataset = params.get("dataset") || "";
  const q = useQuery({
    queryKey: ["experiments", pid, dataset],
    queryFn: () => api<ExperimentSummary[]>(projectPath(pid, `/experiments${qs({ dataset })}`)),
  });
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(-2)));
  const byId = new Map(q.data?.map((e) => [e.experiment_id, e]));
  const sameVersion = picked.length === 2 && byId.get(picked[0])?.version === byId.get(picked[1])?.version;
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Experiments</h1>
          <div className="muted small">
            Runs of a target over a dataset version, from <code>tirkit run</code>, <code>tirkit.runner.run()</code> or the playground. Means show 95% bootstrap CIs.
            {dataset && (
              <>
                {" "}Filtered to <b>{dataset}</b> · <Link to={`/p/${pid}/experiments`}>show all</Link>
              </>
            )}
          </div>
        </div>
        <button
          className="primary"
          disabled={!sameVersion}
          title={picked.length === 2 && !sameVersion ? "Pick two runs on the same dataset version" : "Pick two runs"}
          onClick={() => nav(`/p/${pid}/compare?baseline=${picked[0]}&candidate=${picked[1]}`)}
        >
          Compare selected ({picked.length}/2)
        </button>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 28 }} />
                <th>Run</th>
                <th>Dataset</th>
                <th>Target</th>
                <th>Scores (mean, 95% CI)</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {q.data?.map((e) => (
                <tr key={e.experiment_id} className="clickable" onClick={() => nav(`/p/${pid}/experiments/${e.experiment_id}`)}>
                  <td onClick={(ev) => ev.stopPropagation()}>
                    <input type="checkbox" aria-label="select run" checked={picked.includes(e.experiment_id)} onChange={() => toggle(e.experiment_id)} />
                  </td>
                  <td>
                    <b>{e.name || e.experiment_id}</b>
                    <div className="small muted mono">{e.experiment_id}</div>
                  </td>
                  <td className="small">
                    {e.dataset} <span className="mono muted">@{e.version}</span>
                    <div className="muted">{e.n_items} items</div>
                  </td>
                  <td className="small mono">{e.target}</td>
                  <td className="small">
                    {Object.entries(e.summary ?? {}).map(([name, s]) => (
                      <div key={name}>
                        {name}: <b>{s.mean?.toFixed(3) ?? "—"}</b> <span className="muted">{ci(s.ci)}</span>
                        {s.failed > 0 && <span className="badge warn" style={{ marginLeft: 4 }}>{s.failed} failed</span>}
                      </div>
                    ))}
                  </td>
                  <td className="small muted">{fmtAgo(e.created)}</td>
                </tr>
              ))}
              {q.data && !q.data.length && (
                <tr>
                  <td colSpan={6}>
                    <EmptyState
                      title="Run your first experiment"
                      code={
                        <CodeBlock
                          code={`# 1. a dataset: Datasets > Build from recent traces, then export it (or use any JSONL file)
# 2. score your app over it
export TIRKIT_HOST=${window.location.origin}   # plus TIRKIT_PUBLIC_KEY and TIRKIT_SECRET_KEY
tirkit run dataset.jsonl app:answer -s exact_match --out baseline.json`}
                        />
                      }
                    >
                      <p>An experiment scores a prompt or model over a dataset. It appears here, and you can compare it with the next one. You can also start one from the <Link to={`/p/${pid}/playground`}>Playground</Link>.</p>
                    </EmptyState>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export function ExperimentView() {
  const pid = usePid();
  const { eid = "" } = useParams();
  const q = useQuery({ queryKey: ["experiment", pid, eid], queryFn: () => api<ExperimentDetail>(projectPath(pid, `/experiments/${eid}`)) });
  const [open, setOpen] = useState<number | null>(null);
  if (!q.data) return <div className="muted">{q.error ? String(q.error) : "Loading…"}</div>;
  const e = q.data;
  const scorers = Object.keys(e.scores);
  return (
    <div>
      <div className="small muted">
        <Link to={`/p/${pid}/experiments`}>Experiments</Link> / <span className="mono">{eid}</span>
      </div>
      <h1>{e.name || e.experiment_id}</h1>
      <div className="small muted" style={{ marginBottom: 12 }}>
        <Link to={`/p/${pid}/datasets/${encodeURIComponent(e.dataset)}?version=${e.dataset_version}`}>{e.dataset}@{e.dataset_version}</Link> · target{" "}
        <span className="mono">{e.target}</span>
      </div>
      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        {Object.entries(e.summary).map(([name, s]) => (
          <div key={name} className="panel stat">
            <div className="label">{name}</div>
            <div className="value">{s.mean?.toFixed(3) ?? "—"}</div>
            <div className="sub">
              95% CI {ci(s.ci)} · n={s.n}
              {s.failed ? ` · ${s.failed} failed` : ""}
            </div>
          </div>
        ))}
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Input</th>
              <th>Output</th>
              {scorers.map((s) => (
                <th key={s} className="num">{s}</th>
              ))}
              <th className="num">Latency</th>
            </tr>
          </thead>
          <tbody>
            {e.outputs.map((out, i) => (
              <Fragment key={i}>
                <tr className="clickable" onClick={() => setOpen(open === i ? null : i)}>
                  <td className="muted">{i}</td>
                  <td className="truncate">{preview(e.examples[i]?.input)}</td>
                  <td className="truncate">{e.errors[i] ? <span className="badge bad">{e.errors[i]}</span> : preview(out)}</td>
                  {scorers.map((s) => (
                    <td key={s} className="num" title={e.reasons[s]?.[i] ?? ""}>
                      {e.scores[s][i] === null ? <span className="badge warn">—</span> : e.scores[s][i]!.toFixed(2)}
                    </td>
                  ))}
                  <td className="num">{fmtMs(e.latencies_ms[i])}</td>
                </tr>
                {open === i && (
                  <tr>
                    <td />
                    <td colSpan={scorers.length + 3}>
                      <div className="grid grid-2">
                        <div>
                          <h3>Input</h3>
                          <Content value={e.examples[i]?.input ?? null} pid={pid} />
                          <h3 style={{ marginTop: 8 }}>Expected</h3>
                          <Content value={e.examples[i]?.expected ?? null} pid={pid} />
                        </div>
                        <div>
                          <h3>Output</h3>
                          <Content value={out} pid={pid} />
                          {scorers.map((s) => (
                            <div key={s} className="small" style={{ marginTop: 6 }}>
                              <b>{s}</b>: {e.reasons[s]?.[i]}
                            </div>
                          ))}
                          {e.trace_ids[i] && (
                            <Link className="small" to={`/p/${pid}/traces/${e.trace_ids[i]}`}>Open trace</Link>
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
    </div>
  );
}

type FilterKind = "all" | "regressions" | "improvements" | "changed";

export function Compare() {
  const pid = usePid();
  const [params] = useSearchParams();
  const baseline = params.get("baseline") || "";
  const candidate = params.get("candidate") || "";
  const q = useQuery({
    queryKey: ["compare", pid, baseline, candidate],
    queryFn: () => api<CompareResult>(projectPath(pid, `/compare${qs({ baseline, candidate })}`)),
    enabled: !!baseline && !!candidate,
  });
  const [scorer, setScorer] = useState("");
  const [filter, setFilter] = useState<FilterKind>("regressions");
  const [open, setOpen] = useState<number | null>(null);
  const comps = q.data?.comparisons ?? [];
  const active = scorer || comps.find((c) => c.verdict === "worse")?.scorer || comps[0]?.scorer || "";
  const comp = comps.find((c) => c.scorer === active);
  const items = useMemo(() => {
    if (!q.data || !comp) return [];
    const sign = comp.higher_is_better ? 1 : -1;
    return q.data.items
      .map((it) => {
        const a = it.baseline.scores[active];
        const b = it.candidate.scores[active];
        const d = a === null || b === null || a === undefined || b === undefined ? null : sign * (b - a);
        return { it, a, b, d };
      })
      .filter(({ d }) =>
        filter === "all" ? true : d === null ? false : filter === "regressions" ? d < 0 : filter === "improvements" ? d > 0 : d !== 0,
      )
      .sort((x, y) => (x.d ?? 0) - (y.d ?? 0));
  }, [q.data, comp, active, filter]);
  if (!baseline || !candidate) return <div className="empty">Pick two experiments on the Experiments page to compare.</div>;
  if (q.error) return <div className="error-box">{String(q.error)}</div>;
  if (!q.data) return <div className="muted">Loading…</div>;
  const scale = Math.max(...comps.map((c) => Math.max(Math.abs(c.diff_ci[0]), Math.abs(c.diff_ci[1]))), 1e-6);
  return (
    <div>
      <div className="small muted">
        <Link to={`/p/${pid}/experiments`}>Experiments</Link> / compare
      </div>
      <h1>Compare</h1>
      <div className="small muted" style={{ marginBottom: 12 }}>
        Baseline <Link className="mono" to={`/p/${pid}/experiments/${baseline}`}>{baseline}</Link> vs candidate{" "}
        <Link className="mono" to={`/p/${pid}/experiments/${candidate}`}>{candidate}</Link>. Paired differences (candidate − baseline) with 95% bootstrap CIs. A
        verdict is only given when the CI excludes zero.
      </div>
      <div className="panel" style={{ marginBottom: 14, padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Scorer</th>
              <th className="num">Baseline</th>
              <th className="num">Candidate</th>
              <th className="num">Difference</th>
              <th>95% CI of difference</th>
              <th>Verdict</th>
              <th className="num">n</th>
            </tr>
          </thead>
          <tbody>
            {comps.map((c) => (
              <tr key={c.scorer} className="clickable" onClick={() => setScorer(c.scorer)} style={c.scorer === active ? { background: "var(--accent-soft)" } : undefined}>
                <td>
                  <b>{c.scorer}</b> {!c.higher_is_better && <span className="small muted">(lower is better)</span>}
                </td>
                <td className="num">
                  {c.mean_baseline.toFixed(3)} <div className="small muted">{ci(c.ci_baseline)}</div>
                </td>
                <td className="num">
                  {c.mean_candidate.toFixed(3)} <div className="small muted">{ci(c.ci_candidate)}</div>
                </td>
                <td className={`num ${c.verdict === "better" ? "diff-up" : c.verdict === "worse" ? "diff-down" : ""}`}>
                  {c.diff >= 0 ? "+" : ""}
                  {c.diff.toFixed(3)}
                </td>
                <td>
                  <CIBar diff={c.diff} ci={c.diff_ci} higherIsBetter={c.higher_is_better} scale={scale} />
                  <div className="small muted">{ci(c.diff_ci)}</div>
                </td>
                <td>
                  <span className={`badge ${c.verdict === "better" ? "good" : c.verdict === "worse" ? "bad" : ""}`}>{c.verdict}</span>
                </td>
                <td className="num">
                  {c.n}
                  {c.excluded > 0 && <div className="small muted">{c.excluded} excluded</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Items · {active}</h2>
        <div className="seg">
          {(["regressions", "improvements", "changed", "all"] as FilterKind[]).map((f) => (
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
              {f}
            </button>
          ))}
        </div>
        <span className="small muted">{items.length} items</span>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Input</th>
              <th>Baseline output</th>
              <th>Candidate output</th>
              <th className="num">Base</th>
              <th className="num">Cand</th>
            </tr>
          </thead>
          <tbody>
            {items.map(({ it, a, b, d }) => (
              <Fragment key={it.idx}>
                <tr className="clickable" onClick={() => setOpen(open === it.idx ? null : it.idx)}>
                  <td className="muted">{it.idx}</td>
                  <td className="truncate" style={{ maxWidth: 220 }}>{preview(it.example?.input)}</td>
                  <td className="truncate" style={{ maxWidth: 260 }}>{preview(it.baseline.output)}</td>
                  <td className="truncate" style={{ maxWidth: 260 }}>{preview(it.candidate.output)}</td>
                  <td className="num">{a?.toFixed(2) ?? "—"}</td>
                  <td className={`num ${d !== null && d < 0 ? "diff-down" : d !== null && d > 0 ? "diff-up" : ""}`}>{b?.toFixed(2) ?? "—"}</td>
                </tr>
                {open === it.idx && (
                  <tr>
                    <td />
                    <td colSpan={5}>
                      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
                        <div>
                          <h3>Input</h3>
                          <Content value={(it.example?.input ?? null) as Json} pid={pid} />
                          <h3 style={{ marginTop: 8 }}>Expected</h3>
                          <Content value={it.example?.expected ?? null} pid={pid} />
                        </div>
                        <div>
                          <h3>Baseline</h3>
                          <Content value={it.baseline.output} pid={pid} />
                          {it.baseline.trace_id && <Link className="small" to={`/p/${pid}/traces/${it.baseline.trace_id}`}>trace</Link>}
                        </div>
                        <div>
                          <h3>Candidate</h3>
                          <Content value={it.candidate.output} pid={pid} />
                          {it.candidate.trace_id && <Link className="small" to={`/p/${pid}/traces/${it.candidate.trace_id}`}>trace</Link>}
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
      <details style={{ marginTop: 12 }}>
        <summary>Markdown report (paste into a PR)</summary>
        <pre className="json">{q.data.markdown}</pre>
      </details>
    </div>
  );
}
