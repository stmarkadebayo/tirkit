// Score averages over time, one row per period, with each score's 95% interval beside its mean.
import type { Metrics } from "./api";
import { ciKey } from "./components/Charts";

export type TrendRow = Record<string, number | [number, number]>;

export function mergeScores(m: Pick<Metrics, "scores">): TrendRow[] {
  const byT = new Map<number, TrendRow>();
  for (const [name, pts] of Object.entries(m.scores)) {
    for (const p of pts) {
      const row = byT.get(p.t) ?? { t: p.t };
      row[name] = p.mean;
      if (p.ci_low != null && p.ci_high != null) row[ciKey(name)] = [p.ci_low, p.ci_high];
      byT.set(p.t, row);
    }
  }
  return [...byT.values()].sort((a, b) => (a.t as number) - (b.t as number));
}
