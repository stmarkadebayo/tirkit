import { Area, AreaChart, CartesianGrid, ErrorBar, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

/** Data key holding a series' 95% interval as [low, high] (drawn as whiskers when `intervals`). */
export const ciKey = (key: string) => `${key}__ci`;
const errKey = (key: string) => `${key}__err`;

type Datum = Record<string, number | [number, number]>;

/** Recharts error bars take distances below and above the value, not the interval's ends. */
export function withErrors(data: Datum[], series: { key: string }[]): Datum[] {
  return data.map((row) => {
    const out: Datum = { ...row };
    for (const s of series) {
      const v = row[s.key];
      const ci = row[ciKey(s.key)];
      if (typeof v === "number" && Array.isArray(ci)) out[errKey(s.key)] = [v - ci[0], ci[1] - v];
    }
    return out;
  });
}

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
  intervals = false,
  height = 200,
}: {
  data: Datum[];
  series: SeriesDef[];
  span: number;
  format?: (v: number) => string;
  area?: boolean;
  /** Draw each series' 95% interval (data key `ciKey(key)`) as whiskers on its points (visible even
   *  for a single period) and add it to the tooltip. */
  intervals?: boolean;
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
      formatter={(v: number, _name: string, item: { dataKey?: unknown; payload?: Datum }) => {
        const ci = intervals && typeof item?.dataKey === "string" ? item.payload?.[ciKey(item.dataKey)] : undefined;
        return Array.isArray(ci) ? `${format(v)} (95% ${format(ci[0])} to ${format(ci[1])})` : format(v);
      }}
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
      ) : intervals ? (
        <LineChart {...common} data={withErrors(data, series)}>
          {axes}
          {series.map((s, i) => (
            <Line key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color || COLORS[i % 5]} dot={{ r: 2 }} strokeWidth={2} isAnimationActive={false} connectNulls>
              <ErrorBar dataKey={errKey(s.key)} width={4} strokeWidth={1} stroke={s.color || COLORS[i % 5]} direction="y" />
            </Line>
          ))}
        </LineChart>
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
