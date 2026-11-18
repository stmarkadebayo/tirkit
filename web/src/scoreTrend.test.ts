import { describe, expect, it } from "vitest";
import { mergeScores } from "./scoreTrend";
import { withErrors } from "./components/Charts";

describe("mergeScores", () => {
  it("puts each score's interval beside its mean, and leaves it out below two scores", () => {
    const rows = mergeScores({
      scores: {
        quality: [
          { bucket: 1, t: 200, mean: 0.5, n: 10, ci_low: 0.3, ci_high: 0.7 },
          { bucket: 0, t: 100, mean: 0.8, n: 1, ci_low: null, ci_high: null },
        ],
        helpful: [{ bucket: 1, t: 200, mean: 1, n: 4, ci_low: 0.9, ci_high: 1.1 }],
      },
    });
    expect(rows.map((r) => r.t)).toEqual([100, 200]);
    expect(rows[0]).toEqual({ t: 100, quality: 0.8 });
    expect(rows[1]).toEqual({ t: 200, quality: 0.5, quality__ci: [0.3, 0.7], helpful: 1, helpful__ci: [0.9, 1.1] });
  });
});

describe("withErrors", () => {
  it("turns an interval into distances below and above the value", () => {
    const rows = withErrors([{ t: 1, q: 0.5, q__ci: [0.3, 0.8] }, { t: 2, q: 0.4 }], [{ key: "q" }]);
    const [below, above] = rows[0].q__err as [number, number];
    expect(below).toBeCloseTo(0.2);
    expect(above).toBeCloseTo(0.3);
    expect(rows[1]).toEqual({ t: 2, q: 0.4 });
  });
});
