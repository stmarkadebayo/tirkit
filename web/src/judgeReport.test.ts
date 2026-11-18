import { describe, expect, it } from "vitest";
import { flipVerdict, kappaVerdict, labelsProgress, lengthVerdict, nextAction, readReport, wilson, withInterval } from "./judgeReport";

const rd = (s: Record<string, number | null>) => readReport(s);

describe("readReport", () => {
  it("keeps missing figures as null, never zero", () => {
    const r = readReport({});
    expect(r.kappa).toBeNull();
    expect(r.flip).toBeNull();
    expect(r.ciLow).toBeNull();
    expect(r.nJudged).toBe(0);
  });
  it("ignores non-numbers", () => {
    expect(readReport({ kappa: "0.5", flip_rate: NaN }).kappa).toBeNull();
    expect(readReport({ kappa: 0.5, flip_rate: NaN }).flip).toBeNull();
  });
});

describe("kappaVerdict", () => {
  it("says not measured when there is no kappa", () => {
    const v = kappaVerdict(rd({}));
    expect(v.level).toBe("unmeasured");
    expect(v.title).toBe("Not measured yet");
  });
  it("refuses to judge from a handful of labels", () => {
    expect(kappaVerdict(rd({ kappa: 0.9, n_judged: 8 })).level).toBe("few");
  });
  it("bands the kappa", () => {
    expect(kappaVerdict(rd({ kappa: 0.2, n_judged: 120 })).level).toBe("poor");
    expect(kappaVerdict(rd({ kappa: 0.5, n_judged: 120 })).level).toBe("fair");
    expect(kappaVerdict(rd({ kappa: 0.7, n_judged: 120 })).level).toBe("good");
  });
  it("warns about a wide interval and about few labels", () => {
    expect(kappaVerdict(rd({ kappa: 0.7, n_judged: 120, kappa_ci_low: 0.3, kappa_ci_high: 0.9 })).text).toContain("interval is wide");
    expect(kappaVerdict(rd({ kappa: 0.7, n_judged: 40 })).text).toContain("rests on 40 labels");
  });
});

describe("flipVerdict and lengthVerdict", () => {
  it("is unmeasured without pairs", () => {
    expect(flipVerdict(null, 0).level).toBe("unmeasured");
    expect(flipVerdict(0.5, 0).level).toBe("unmeasured");
  });
  it("bands the flip rate", () => {
    expect(flipVerdict(0.05, 30).level).toBe("good");
    expect(flipVerdict(0.2, 30).level).toBe("fair");
    expect(flipVerdict(0.6, 30).level).toBe("poor");
  });
  it("compares judge and human length correlation", () => {
    expect(lengthVerdict(0.8, 0.2).level).toBe("poor");
    expect(lengthVerdict(0.3, 0.25).level).toBe("good");
    expect(lengthVerdict(null, 0.2).level).toBe("unmeasured");
  });
});

describe("wilson", () => {
  it("brackets the proportion and narrows with n", () => {
    const [lo, hi] = wilson(0.2, 50)!;
    expect(lo).toBeLessThan(0.2);
    expect(hi).toBeGreaterThan(0.2);
    const [lo2, hi2] = wilson(0.2, 500)!;
    expect(hi2 - lo2).toBeLessThan(hi - lo);
  });
  it("stays inside 0..1 and has no interval without pairs", () => {
    const [lo, hi] = wilson(0, 10)!;
    expect(lo).toBe(0);
    expect(hi).toBeGreaterThan(0);
    expect(wilson(0.5, 0)).toBeNull();
  });
});

describe("labels and next action", () => {
  it("counts labels out of 100", () => {
    expect(labelsProgress(37).text).toBe("37 of 100 labels");
    expect(labelsProgress(130).text).toContain("enough");
  });
  it("sends you to label first, then to run a report, then to the rubric", () => {
    expect(nextAction(5, false, null).to).toBe("annotation");
    expect(nextAction(30, false, null).to).toBe("judge#run");
    expect(nextAction(30, true, readReport({ kappa: 0.3, n_judged: 30 })).to).toBe("judge#rubric");
    expect(nextAction(60, true, readReport({ kappa: 0.8, n_judged: 60 })).to).toBe("annotation");
    expect(nextAction(150, true, readReport({ kappa: 0.8, n_judged: 150 })).to).toBe("experiments");
  });
});

describe("intervals in a report", () => {
  it("reads every stored interval", () => {
    const r = readReport({
      weighted_kappa: 0.6, weighted_kappa_ci_low: 0.5, weighted_kappa_ci_high: 0.7,
      pass_fail_kappa: 0.4, pass_fail_kappa_ci_low: 0.2, pass_fail_kappa_ci_high: 0.6,
      exact_agreement: 0.5, exact_agreement_ci_low: 0.4, exact_agreement_ci_high: 0.6,
      length_bias: 0.1, length_bias_ci_low: -0.1, length_bias_ci_high: 0.3,
      flip_rate: 0.3, n_pairs_decided: 40, flip_rate_ci_low: 0.18, flip_rate_ci_high: 0.45,
    });
    expect(r.weightedCi).toEqual([0.5, 0.7]);
    expect(r.passFailCi).toEqual([0.2, 0.6]);
    expect(r.exactCi).toEqual([0.4, 0.6]);
    expect(r.lengthBias).toBe(0.1);
    expect(r.lengthBiasCi).toEqual([-0.1, 0.3]);
    expect(r.flipCi).toEqual([0.18, 0.45]);
  });
  it("falls back for reports made before intervals were stored", () => {
    const r = readReport({ flip_rate: 0.3, n_pairs_decided: 40, length_corr_judge: 0.5, length_corr_human: 0.2 });
    expect(r.flipCi).toEqual(wilson(0.3, 40));
    expect(r.lengthBias).toBeCloseTo(0.3);
    expect(r.lengthBiasCi).toBeNull();
    expect(r.weightedCi).toBeNull();
  });
  it("formats a value with its interval, and says when there is none", () => {
    expect(withInterval(0.42, [0.31, 0.55])).toBe("0.42 (95% 0.31 to 0.55)");
    expect(withInterval(0.42, null)).toBe("0.42 (no interval recorded)");
    expect(withInterval(null, null)).toBe("not measured yet");
  });
});
