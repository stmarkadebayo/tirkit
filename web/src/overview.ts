// What the Overview page says about experiments and the CI gate. Pure functions over API data.

import type { Comparison, ExperimentSummary } from "./api";

/** The newest finished experiment and the one before it on the same dataset version (what a gate compares). */
export function latestPair(experiments: ExperimentSummary[]): { candidate: ExperimentSummary; baseline: ExperimentSummary | null } | null {
  const done = experiments.filter((e) => e.status === "done" || e.status === "finished" || e.finished !== null).sort((a, b) => b.created - a.created);
  const candidate = done[0];
  if (!candidate) return null;
  const baseline = done.slice(1).find((e) => e.dataset === candidate.dataset && e.version === candidate.version) ?? null;
  return { candidate, baseline };
}

export type GateState = "pass" | "fail" | "none";

export interface Gate {
  state: GateState;
  title: string;
  text: string;
  /** Scorers the candidate is confidently worse on. */
  failing: string[];
}

/** The gate rule of `tirkit gate` with no allowed drop: fail only if the candidate is confidently worse. */
export function gateFromComparisons(comps: Comparison[] | null | undefined): Gate {
  if (!comps || !comps.length) {
    return { state: "none", title: "No gate result yet", text: "Run two experiments on the same dataset version to see whether the newest one would pass.", failing: [] };
  }
  const failing = comps.filter((c) => c.verdict === "worse").map((c) => c.scorer);
  if (failing.length) {
    return { state: "fail", title: "Would fail the gate", text: `The newest run is confidently worse on ${failing.join(", ")}.`, failing };
  }
  const better = comps.filter((c) => c.verdict === "better").length;
  return {
    state: "pass",
    title: "Would pass the gate",
    text: better ? `Not worse on any scorer, and better on ${better}.` : "Not confidently worse on any scorer. Where it says no clear difference, the data cannot tell the runs apart.",
    failing: [],
  };
}
