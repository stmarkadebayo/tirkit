// Reads a stored judge report (a loose summary object) and turns it into plain-language results.
// Nothing here invents a number: a missing figure stays null and the UI says "not measured yet".
// The thresholds are common conventions (Landis and Koch for kappa), shown to the user as such.

export const LABELS_NEEDED = 100;
export const MIN_LABELS_FOR_A_READING = 20;

export type Summary = Record<string, number | string | null | undefined>;

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export interface Reading {
  kappa: number | null;
  ciLow: number | null;
  ciHigh: number | null;
  nJudged: number;
  nLabelled: number;
  exact: number | null;
  weighted: number | null;
  passFail: number | null;
  flip: number | null;
  nPairs: number;
  lengthJudge: number | null;
  lengthHuman: number | null;
  /** Judge minus human length correlation (stored, or derived for older reports). */
  lengthBias: number | null;
  /** 95% intervals stored with the report; null for reports made before they were recorded. */
  weightedCi: Interval | null;
  passFailCi: Interval | null;
  exactCi: Interval | null;
  lengthBiasCi: Interval | null;
  /** Stored Wilson interval; for older reports, computed here from the rate and pair count. */
  flipCi: Interval | null;
}

export type Interval = [number, number];

function interval(s: Summary, key: string): Interval | null {
  const lo = num(s[`${key}_ci_low`]);
  const hi = num(s[`${key}_ci_high`]);
  return lo === null || hi === null ? null : [lo, hi];
}

/** "0.42 (95% 0.31 to 0.55)", or the value alone when no interval was recorded. */
export function withInterval(value: number | null, ci: Interval | null, digits = 2): string {
  if (value === null) return "not measured yet";
  const v = value.toFixed(digits);
  return ci ? `${v} (95% ${ci[0].toFixed(digits)} to ${ci[1].toFixed(digits)})` : `${v} (no interval recorded)`;
}

export function readReport(summary: Summary | null | undefined): Reading {
  const s = summary ?? {};
  const flip = num(s.flip_rate);
  const nPairs = num(s.n_pairs_decided) ?? 0;
  const lj = num(s.length_corr_judge);
  const lh = num(s.length_corr_human);
  return {
    lengthBias: num(s.length_bias) ?? (lj !== null && lh !== null ? lj - lh : null),
    weightedCi: interval(s, "weighted_kappa"),
    passFailCi: interval(s, "pass_fail_kappa"),
    exactCi: interval(s, "exact_agreement"),
    lengthBiasCi: interval(s, "length_bias"),
    flipCi: interval(s, "flip_rate") ?? (flip !== null ? wilson(flip, nPairs) : null),
    kappa: num(s.kappa),
    ciLow: num(s.kappa_ci_low),
    ciHigh: num(s.kappa_ci_high),
    nJudged: num(s.n_judged) ?? 0,
    nLabelled: num(s.n_labelled) ?? 0,
    exact: num(s.exact_agreement),
    weighted: num(s.weighted_kappa),
    passFail: num(s.pass_fail_kappa),
    flip: num(s.flip_rate),
    nPairs: num(s.n_pairs_decided) ?? 0,
    lengthJudge: num(s.length_corr_judge),
    lengthHuman: num(s.length_corr_human),
  };
}

export type Level = "unmeasured" | "few" | "poor" | "fair" | "good";

export interface Verdict {
  level: Level;
  title: string;
  text: string;
}

export function kappaVerdict(r: Reading): Verdict {
  if (r.kappa === null) {
    return { level: "unmeasured", title: "Not measured yet", text: "Run the judge against your human labels to measure how well it agrees with you." };
  }
  if (r.nJudged < MIN_LABELS_FOR_A_READING) {
    return {
      level: "few",
      title: "Too few labels to say",
      text: `Only ${r.nJudged} labelled items were judged. Label at least ${MIN_LABELS_FOR_A_READING} for a first reading and about ${LABELS_NEEDED} for a firm one.`,
    };
  }
  const wide = r.ciLow !== null && r.ciHigh !== null && r.ciHigh - r.ciLow > 0.3;
  const extra = wide ? " The interval is wide, so the true agreement could be quite different; more labels would narrow it." : r.nJudged < LABELS_NEEDED ? ` This rests on ${r.nJudged} labels; ${LABELS_NEEDED} or more give a firmer answer.` : "";
  if (r.kappa < 0.4) {
    return { level: "poor", title: "Not reliable yet", text: `The judge agrees with your labels little more than chance would. Do not rely on its scores yet.${extra}` };
  }
  if (r.kappa < 0.6) {
    return { level: "fair", title: "Some agreement", text: `Good enough to notice large changes, not small ones. Improve the rubric and measure again.${extra}` };
  }
  return { level: "good", title: "Agrees well with you", text: `The judge's scores track your labels well enough to use for comparing changes.${extra}` };
}

export function flipVerdict(rate: number | null, nPairs: number): Verdict {
  if (rate === null || nPairs === 0) {
    return { level: "unmeasured", title: "Not measured yet", text: "Needs pairs of answers judged in both orders." };
  }
  if (rate <= 0.1) return { level: "good", title: "Order rarely matters", text: "Swapping the order of two answers seldom changes the verdict." };
  if (rate <= 0.25) return { level: "fair", title: "Order sometimes matters", text: "Some verdicts change when the answers swap places. Compare in both orders and average." };
  return { level: "poor", title: "Order matters a lot", text: "The verdict often follows position, not quality. Do not use single-order comparisons." };
}

export function lengthVerdict(judge: number | null, human: number | null): Verdict {
  if (judge === null || human === null) {
    return { level: "unmeasured", title: "Not measured yet", text: "Needs at least three judged items." };
  }
  const gap = judge - human;
  if (gap > 0.2) return { level: "poor", title: "Judge favours long answers", text: "The judge rewards length more than your labels do." };
  if (gap < -0.2) return { level: "fair", title: "Judge favours short answers", text: "The judge rewards length less than your labels do." };
  return { level: "good", title: "No extra length bias", text: "The judge's link to answer length is close to yours." };
}

/** 95% Wilson score interval for a proportion k/n; null when n is 0. */
export function wilson(p: number, n: number): [number, number] | null {
  if (n <= 0) return null;
  const z = 1.96;
  const denom = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / denom;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / denom;
  return [Math.max(0, centre - half), Math.min(1, centre + half)];
}

export function labelsProgress(done: number): { done: number; needed: number; text: string } {
  const text = done >= LABELS_NEEDED ? `${done} labels, enough for a firm reading` : `${done} of ${LABELS_NEEDED} labels`;
  return { done, needed: LABELS_NEEDED, text };
}

/** The next thing to do, in one sentence and a place to do it (a page under /p/:pid/). */
export function nextAction(labels: number, hasReport: boolean, r: Reading | null): { text: string; to: string; cta: string } {
  if (labels < MIN_LABELS_FOR_A_READING) {
    return { text: `Label ${MIN_LABELS_FOR_A_READING - labels} more items, then run the judge against them.`, to: "annotation", cta: "Open the annotation queue" };
  }
  if (!hasReport || !r || r.kappa === null) {
    return { text: "You have enough labels for a first reading. Run the judge against them.", to: "judge#run", cta: "Run a report" };
  }
  const v = kappaVerdict(r);
  if (v.level === "poor" || v.level === "fair") {
    return { text: "Edit the rubric (explicit criteria, worked examples) and run the report again. Every rubric version keeps its numbers.", to: "judge#rubric", cta: "Edit the rubric" };
  }
  if (labels < LABELS_NEEDED) {
    return { text: `Label ${LABELS_NEEDED - labels} more items to firm up the interval.`, to: "annotation", cta: "Open the annotation queue" };
  }
  return { text: "The judge is reliable on this rubric. Use it in experiments and online evals, and re-check when the rubric or model changes.", to: "experiments", cta: "Go to experiments" };
}
