import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, projectPath, qs, type Metrics } from "../api";
import { RANGES, usePid } from "../hooks";
import { TimeChart } from "../components/Charts";
import { fmtCompact, fmtCost, fmtMs, fmtNum } from "../format";
import { Stat } from "../components/Stat";
import { mergeScores } from "../scoreTrend";

export function RangePicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="seg" role="group" aria-label="Time range">
      {Object.keys(RANGES).map((r) => (
        <button key={r} className={r === value ? "on" : ""} onClick={() => onChange(r)}>
          {r}
        </button>
      ))}
    </div>
  );
}

export default function Dashboard() {
  const pid = usePid();
  const [range, setRange] = useState("24h");
  const [groupBy, setGroupBy] = useState("model");
  const [now] = useState(() => Math.floor(Date.now() / 1000));
  const since = now - RANGES[range];
  const m = useQuery({
    queryKey: ["metrics", pid, range],
    queryFn: () => api<Metrics>(projectPath(pid, `/metrics${qs({ since, until: now, buckets: 48 })}`)),
  });
  const grouped = useQuery({
    queryKey: ["metrics", pid, range, groupBy],
    queryFn: () => api<Metrics>(projectPath(pid, `/metrics${qs({ since, until: now, buckets: 1, group_by: groupBy })}`)),
  });
  const pts = m.data?.series.all ?? [];
  const totals = useMemo(() => {
    const t = { spans: 0, errors: 0, traces: 0, trace_errors: 0, cost: 0, tin: 0, tout: 0 };
    for (const p of pts) {
      t.spans += p.spans;
      t.errors += p.errors;
      t.traces += p.traces ?? 0;
      t.trace_errors += p.trace_errors ?? 0;
      t.cost += p.cost_usd ?? 0;
      t.tin += p.input_tokens ?? 0;
      t.tout += p.output_tokens ?? 0;
    }
    return t;
  }, [pts]);
  const p95 = useMemo(() => {
    const vals = pts.filter((p) => p.p95_ms !== undefined).map((p) => p.p95_ms!);
    return vals.length ? Math.max(...vals) : null;
  }, [pts]);
  const span = RANGES[range];
  const data = pts.map((p) => ({
    t: p.t,
    traces: p.traces ?? 0,
    errors: p.trace_errors ?? 0,
    cost: p.cost_usd ?? 0,
    input: p.input_tokens ?? 0,
    output: p.output_tokens ?? 0,
    p50: p.p50_ms ?? NaN,
    p95: p.p95_ms ?? NaN,
    p99: p.p99_ms ?? NaN,
    error_rate: p.traces ? (100 * (p.trace_errors ?? 0)) / p.traces : NaN,
  })) as unknown as Record<string, number>[];
  const scoreNames = Object.keys(m.data?.scores ?? {});
  const groups = Object.entries(grouped.data?.series ?? {})
    .map(([name, ps]) => ({
      name,
      spans: ps.reduce((a, p) => a + p.spans, 0),
      errors: ps.reduce((a, p) => a + p.errors, 0),
      cost: ps.reduce((a, p) => a + (p.cost_usd ?? 0), 0),
      tokens: ps.reduce((a, p) => a + (p.input_tokens ?? 0) + (p.output_tokens ?? 0), 0),
      p95: ps[0]?.p95_ms,
    }))
    .sort((a, b) => b.spans - a.spans);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <div className="muted small">Traffic, cost, latency and quality for this project.</div>
        </div>
        <RangePicker value={range} onChange={setRange} />
      </div>
      {m.error && <div className="error-box">{String(m.error)}</div>}
      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat label="Traces" value={fmtNum(totals.traces)} sub={`${fmtNum(totals.spans)} spans`} />
        <Stat label="Cost" value={fmtCost(totals.cost)} sub={totals.traces ? `${fmtCost(totals.cost / totals.traces)} per trace` : ""} />
        <Stat label="Tokens" value={fmtCompact(totals.tin + totals.tout)} sub={`${fmtCompact(totals.tin)} in · ${fmtCompact(totals.tout)} out`} />
        <Stat label="Error rate" value={totals.traces ? `${((100 * totals.trace_errors) / totals.traces).toFixed(1)}%` : "—"} sub={`${fmtNum(totals.trace_errors)} failed traces`} />
        <Stat label="Worst p95 latency" value={fmtMs(p95)} sub={m.data?.latency_sample_capped ? `sample of ${fmtNum(m.data.latency_sample_size)}` : "per bucket, exact"} />
      </div>
      <div className="grid grid-2">
        <div className="panel">
          <h3>Traces</h3>
          <TimeChart data={data} span={span} area series={[{ key: "traces", label: "traces" }, { key: "errors", label: "errors", color: "var(--bad)" }]} />
        </div>
        <div className="panel">
          <h3>Latency</h3>
          <TimeChart data={data} span={span} format={(v) => fmtMs(v)} series={[{ key: "p50", label: "p50" }, { key: "p95", label: "p95" }, { key: "p99", label: "p99" }]} />
        </div>
        <div className="panel">
          <h3>Cost</h3>
          <TimeChart data={data} span={span} area format={(v) => fmtCost(v)} series={[{ key: "cost", label: "USD" }]} />
        </div>
        <div className="panel">
          <h3>Tokens</h3>
          <TimeChart data={data} span={span} area format={(v) => fmtCompact(v)} series={[{ key: "input", label: "input" }, { key: "output", label: "output" }]} />
        </div>
        {scoreNames.length > 0 && (
          <div className="panel">
            <h3>Scores (online evals, feedback, labels)</h3>
            <TimeChart
              data={mergeScores(m.data!)}
              span={span}
              intervals
              format={(v) => v.toFixed(2)}
              series={scoreNames.map((n) => ({ key: n, label: n }))}
            />
            <div className="small muted">Points: average per period. Whiskers: its 95% interval (shown from two scores up). Where whiskers overlap, the change may be noise.</div>
          </div>
        )}
      </div>
      <div className="panel" style={{ marginTop: 14 }}>
        <div className="row" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>Breakdown</h3>
          <div className="spacer" />
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value)} aria-label="Group by">
            <option value="model">by model</option>
            <option value="name">by span name</option>
            <option value="kind">by kind</option>
            <option value="release">by release</option>
            <option value="user_id">by user</option>
          </select>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{groupBy}</th>
                <th className="num">Spans</th>
                <th className="num">Errors</th>
                <th className="num">Tokens</th>
                <th className="num">Cost</th>
                <th className="num">p95 (traces)</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.name}>
                  <td>{g.name}</td>
                  <td className="num">{fmtNum(g.spans)}</td>
                  <td className="num">{fmtNum(g.errors)}</td>
                  <td className="num">{fmtCompact(g.tokens)}</td>
                  <td className="num">{fmtCost(g.cost)}</td>
                  <td className="num">{fmtMs(g.p95)}</td>
                </tr>
              ))}
              {!groups.length && (
                <tr>
                  <td colSpan={6} className="empty">No data yet. Send traces with the SDK or OpenTelemetry (see Settings → API keys).</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
