import { Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export const COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];

export interface SeriesDef {
  key: string;
  label: string;
  color?: string;
}

const tickTime = (span: number) => (t: number) =>
  new Date(t * 1000).toLocaleString(undefined, span > 2 * 86400 ? { month: "short", day: "numeric" } : { hour: "2-digit", minute: "2-digit" });

export function TimeChart({
  data,
  series,
  span,
  format = (v: number) => String(v),
  area = false,
  height = 200,
}: {
  data: Record<string, number>[];
  series: SeriesDef[];
  span: number;
  format?: (v: number) => string;
  area?: boolean;
  height?: number;
}) {
  if (!data.length) return <div className="empty">No data in this time range.</div>;
  const common = {
    data,
    margin: { top: 6, right: 8, left: 0, bottom: 0 },
  };
  const axes = [
    <CartesianGrid key="g" stroke="var(--border)" vertical={false} />,
    <XAxis key="x" dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={tickTime(span)} stroke="var(--muted)" fontSize={11} />,
    <YAxis key="y" tickFormatter={format} stroke="var(--muted)" fontSize={11} width={64} />,
    <Tooltip
      key="tt"
      contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 }}
      labelFormatter={(t: number) => new Date(t * 1000).toLocaleString()}
      formatter={(v: number) => format(v)}
    />,
    series.length > 1 ? <Legend key="l" wrapperStyle={{ fontSize: 12 }} /> : null,
  ];
  return (
    <ResponsiveContainer width="100%" height={height}>
      {area ? (
        <AreaChart {...common}>
          {axes}
          {series.map((s, i) => (
            <Area key={s.key} type="monotone" dataKey={s.key} name={s.label} stackId="1" stroke={s.color || COLORS[i % 5]} fill={s.color || COLORS[i % 5]} fillOpacity={0.25} isAnimationActive={false} />
          ))}
        </AreaChart>
      ) : (
        <LineChart {...common}>
          {axes}
          {series.map((s, i) => (
            <Line key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color || COLORS[i % 5]} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls />
          ))}
        </LineChart>
      )}
    </ResponsiveContainer>
  );
}

/** Horizontal bar showing a difference and its CI around zero. */
export function CIBar({ diff, ci, higherIsBetter, scale }: { diff: number; ci: [number, number]; higherIsBetter: boolean; scale: number }) {
  const s = Math.max(scale, 1e-9);
  const pos = (x: number) => `${50 + (x / s) * 45}%`;
  const good = higherIsBetter ? ci[0] > 0 : ci[1] < 0;
  const bad = higherIsBetter ? ci[1] < 0 : ci[0] > 0;
  const color = good ? "var(--good)" : bad ? "var(--bad)" : "var(--muted)";
  return (
    <div className="ci-bar" title={`difference ${diff.toFixed(3)}, 95% CI [${ci[0].toFixed(3)}, ${ci[1].toFixed(3)}]`}>
      <div className="zero" style={{ left: "50%" }} />
      <div className="range" style={{ left: pos(ci[0]), width: `calc(${pos(ci[1])} - ${pos(ci[0])})`, background: color, opacity: 0.5 }} />
      <div className="point" style={{ left: `calc(${pos(diff)} - 1px)` }} />
    </div>
  );
}
