import { describe, expect, it } from "vitest";
import { buildTree, type TreeSpan } from "./traceTree";

const sp = (span_id: string, parent_id: string | null, start = 0): TreeSpan => ({ span_id, parent_id, start });
const ids = (spans: TreeSpan[]) => buildTree(spans).map((n) => n.span.span_id);
const depths = (spans: TreeSpan[]) => Object.fromEntries(buildTree(spans).map((n) => [n.span.span_id, n.depth]));

describe("buildTree", () => {
  it("orders children by start time under their parent", () => {
    const spans = [sp("c2", "root", 3), sp("root", null, 0), sp("c1", "root", 1), sp("g1", "c1", 2)];
    expect(ids(spans)).toEqual(["root", "c1", "g1", "c2"]);
    expect(depths(spans)).toEqual({ root: 0, c1: 1, g1: 2, c2: 1 });
  });

  it("keeps a span that is its own parent, as a root", () => {
    const nodes = buildTree([sp("root", null, 0), sp("self", "self", 1), sp("child", "self", 2)]);
    expect(nodes.map((n) => n.span.span_id)).toEqual(["root", "self", "child"]);
    expect(nodes[1]).toMatchObject({ depth: 0, issue: "self-parent" });
    expect(nodes[2]).toMatchObject({ depth: 1 });
    expect(nodes[2].issue).toBeUndefined();
  });

  it("keeps every span of a parent cycle, breaking it at the earliest span", () => {
    // a -> b -> c -> a: no span is reachable from a root, so they used to vanish.
    const spans = [sp("root", null, 0), sp("b", "a", 2), sp("c", "b", 3), sp("a", "c", 1)];
    const nodes = buildTree(spans);
    expect(nodes.map((n) => n.span.span_id)).toEqual(["root", "a", "b", "c"]);
    expect(nodes[1]).toMatchObject({ depth: 0, issue: "cycle" });
    expect(depths(spans)).toEqual({ root: 0, a: 0, b: 1, c: 2 });
  });

  it("keeps a two-span cycle and a cycle with a tail hanging off it", () => {
    const spans = [sp("x", "y", 5), sp("y", "x", 6), sp("tail", "y", 7)];
    expect(ids(spans)).toEqual(["x", "y", "tail"]);
    expect(depths(spans)).toEqual({ x: 0, y: 1, tail: 2 });
  });

  it("shows orphans (parent not in the trace) as roots, flagged", () => {
    const nodes = buildTree([sp("root", null, 0), sp("orphan", "missing", 1), sp("kid", "orphan", 2)]);
    expect(nodes.map((n) => [n.span.span_id, n.depth, n.issue])).toEqual([
      ["root", 0, undefined],
      ["orphan", 0, "orphan"],
      ["kid", 1, undefined],
    ]);
  });

  it("returns every span exactly once, whatever the input", () => {
    const spans = [sp("a", "a"), sp("b", "c"), sp("c", "b"), sp("d", "zz"), sp("e", null), sp("f", "e"), sp("g", "f")];
    const out = ids(spans);
    expect(out).toHaveLength(spans.length);
    expect(new Set(out)).toEqual(new Set(spans.map((s) => s.span_id)));
  });

  it("does not copy arrays or recurse: 10,000 spans in a flat, deep and random shape", () => {
    const n = 10_000;
    const flat = [sp("r", null, 0), ...Array.from({ length: n - 1 }, (_, i) => sp(`s${i}`, "r", n - i))];
    const deep = Array.from({ length: n }, (_, i) => sp(`d${i}`, i ? `d${i - 1}` : null, i));
    let seed = 42;
    const rand = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    const random = Array.from({ length: n }, (_, i) => sp(`n${i}`, i ? `n${Math.floor(rand() * i)}` : null, rand()));
    for (const spans of [flat, deep, random]) {
      const t0 = performance.now();
      const nodes = buildTree(spans);
      const ms = performance.now() - t0;
      expect(nodes).toHaveLength(n);
      expect(new Set(nodes.map((x) => x.span.span_id)).size).toBe(n);
      // The budget for the whole tree's first paint is 300 ms with no task over 50 ms; building
      // the tree is a small part of that. The bound is loose for busy CI machines.
      expect(ms).toBeLessThan(200);
    }
    expect(buildTree(deep)[n - 1]).toMatchObject({ depth: n - 1 });
    expect(buildTree(flat)[1].span.span_id).toBe(`s${n - 2}`); // earliest child first
  });
});
