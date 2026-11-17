import { describe, expect, it } from "vitest";
import { activeMoreCount, clearFilters, hasAnyFilter, range, traceQuery, withParam } from "./traceFilters";

const p = (s: string) => new URLSearchParams(s);

describe("withParam", () => {
  it("sets and removes a filter and resets the page", () => {
    const a = withParam(p("page=3"), "q", "refund");
    expect(a.get("q")).toBe("refund");
    expect(a.has("page")).toBe(false);
    expect(withParam(a, "q", "  ").has("q")).toBe(false);
  });
  it("does not keep the default time range in the URL", () => {
    expect(withParam(p("range=30d"), "range", "7d").has("range")).toBe(false);
    expect(withParam(p(""), "range", "30d").get("range")).toBe("30d");
  });
  it("keeps other filters", () => {
    expect(withParam(p("error=true"), "name", "agent").toString()).toBe("error=true&name=agent");
  });
});

describe("range", () => {
  it("falls back for an unknown range", () => {
    expect(range(p("range=1y"))).toBe("7d");
    expect(range(p("range=all"))).toBe("all");
  });
});

describe("counting and clearing", () => {
  it("counts only the filters behind More filters", () => {
    expect(activeMoreCount(p("q=x&error=true&name=a&min_ms=1000"))).toBe(2);
  });
  it("knows when anything is filtered", () => {
    expect(hasAnyFilter(p(""))).toBe(false);
    expect(hasAnyFilter(p("range=24h"))).toBe(true);
    expect(hasAnyFilter(p("user_id=u1"))).toBe(true);
  });
  it("clears every filter but leaves unrelated params", () => {
    expect(clearFilters(p("q=a&name=b&range=1h&other=1")).toString()).toBe("other=1");
  });
});

describe("traceQuery", () => {
  it("maps URL params to API fields", () => {
    const q = traceQuery(p("q=hi&min_ms=5000&range=1h"), 10_000);
    expect(q.search).toBe("hi");
    expect(q.min_duration_ms).toBe("5000");
    expect(q.start_after).toBe(String(10_000 - 3600));
  });
  it("sends no start for all time", () => {
    expect(traceQuery(p("range=all"), 10_000).start_after).toBe("");
  });
});
