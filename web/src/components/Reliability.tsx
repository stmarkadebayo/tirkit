import type { ReactNode } from "react";
import type { Level, Verdict } from "../judgeReport";

const LEVEL_ICON: Record<Level, string> = { good: "✓", fair: "!", poor: "✕", few: "–", unmeasured: "–" };

/** A verdict as a pill: colour plus an icon and words, never colour alone. */
export function VerdictPill({ verdict }: { verdict: Verdict }) {
  return (
    <span className={`verdict ${verdict.level}`}>
      <span aria-hidden>{LEVEL_ICON[verdict.level]}</span> {verdict.title}
    </span>
  );
}

export interface Band {
  from: number;
  to: number;
  level: Level;
}

interface ScaleProps {
  min: number;
  max: number;
  /** Labelled tick values under the axis. */
  ticks: number[];
  bands?: Band[];
  /** Interval (whisker) around the first point. */
  low?: number | null;
  high?: number | null;
  points: { value: number | null; label: string; kind?: "main" | "other" }[];
  description: string;
}

/** A dot-and-interval chart on a fixed axis. Values outside the axis are pinned to its ends. */
export function IntervalScale({ min, max, ticks, bands = [], low, high, points, description }: ScaleProps) {
  const W = 400;
  const pad = 14;
  const x = (v: number) => pad + ((Math.min(max, Math.max(min, v)) - min) / (max - min)) * (W - 2 * pad);
  return (
    <svg className="scale" viewBox={`0 0 ${W} 46`} role="img" aria-label={description} preserveAspectRatio="xMidYMid meet">
      {bands.map((b, i) => (
        <rect key={i} className={`band ${b.level}`} x={x(b.from)} y={10} width={Math.max(0, x(b.to) - x(b.from))} height={14} />
      ))}
      <line className="axis" x1={x(min)} x2={x(max)} y1={17} y2={17} />
      {low != null && high != null && (
        <g className="whisker">
          <line x1={x(low)} x2={x(high)} y1={17} y2={17} />
          <line x1={x(low)} x2={x(low)} y1={11} y2={23} />
          <line x1={x(high)} x2={x(high)} y1={11} y2={23} />
        </g>
      )}
      {points.map(
        (p, i) =>
          p.value !== null && (
            <circle key={i} className={`dot ${p.kind ?? "main"}`} cx={x(p.value)} cy={17} r={p.kind === "other" ? 4 : 5.5}>
              <title>{p.label}</title>
            </circle>
          ),
      )}
      {ticks.map((t) => (
        <text key={t} className="tick" x={x(t)} y={41} textAnchor="middle">
          {t}
        </text>
      ))}
    </svg>
  );
}

export const KAPPA_BANDS: Band[] = [
  { from: -1, to: 0.4, level: "poor" },
  { from: 0.4, to: 0.6, level: "fair" },
  { from: 0.6, to: 1, level: "good" },
];

/** A labelled progress bar toward a target count. */
export function CountBar({ done, needed, text }: { done: number; needed: number; text: string }) {
  return (
    <div>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={needed} aria-valuenow={Math.min(done, needed)} aria-label="Labels so far">
        <div style={{ width: `${Math.min(100, (100 * done) / needed)}%` }} />
      </div>
      <div className="small muted">{text}</div>
    </div>
  );
}

export function Metric({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="metric">
      <div className="small muted">{label}</div>
      <div className="metric-value">{children}</div>
      {hint && <div className="small muted">{hint}</div>}
    </div>
  );
}

export const fmt2 = (v: number | null) => (v === null ? "not measured yet" : v.toFixed(2));
