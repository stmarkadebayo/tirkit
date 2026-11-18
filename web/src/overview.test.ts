import { describe, expect, it } from "vitest";
import type { Comparison, ExperimentSummary } from "./api";
import { gateFromComparisons, latestPair } from "./overview";

const exp = (id: string, created: number, dataset = "d", version = "v1", finished: number | null = created + 1): ExperimentSummary =>
  ({ experiment_id: id, name: id, dataset, version, target: null, status: "done", created, finished }) as ExperimentSummary;
const cmp = (scorer: string, verdict: string): Comparison =>
  ({ scorer, verdict, n: 10, excluded: 0, mean_baseline: 1, mean_candidate: 1, ci_baseline: [0, 1], ci_candidate: [0, 1], diff: 0, diff_ci: [0, 0], higher_is_better: true }) as Comparison;

describe("latestPair", () => {
  it("has nothing without experiments", () => {
    expect(latestPair([])).toBeNull();
  });
  it("pairs the newest with the previous run on the same dataset version", () => {
    const p = latestPair([exp("a", 1), exp("b", 2, "other"), exp("c", 3)])!;
    expect(p.candidate.experiment_id).toBe("c");
    expect(p.baseline?.experiment_id).toBe("a");
  });
  it("does not pair across dataset versions", () => {
    const p = latestPair([exp("a", 1, "d", "v1"), exp("b", 2, "d", "v2")])!;
    expect(p.baseline).toBeNull();
  });
  it("skips runs that have not finished", () => {
    const p = latestPair([exp("a", 1), { ...exp("b", 2), status: "running", finished: null }])!;
    expect(p.candidate.experiment_id).toBe("a");
  });
});

describe("gateFromComparisons", () => {
  it("has no result without comparisons", () => {
    expect(gateFromComparisons([]).state).toBe("none");
    expect(gateFromComparisons(null).state).toBe("none");
  });
  it("fails when any scorer is confidently worse", () => {
    const g = gateFromComparisons([cmp("exact_match", "better"), cmp("judge:correctness", "worse")]);
    expect(g.state).toBe("fail");
    expect(g.failing).toEqual(["judge:correctness"]);
  });
  it("passes when nothing is confidently worse", () => {
    expect(gateFromComparisons([cmp("a", "no clear difference")]).state).toBe("pass");
    expect(gateFromComparisons([cmp("a", "better")]).text).toContain("better on 1");
  });
});
