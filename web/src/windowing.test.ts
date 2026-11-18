import { describe, expect, it } from "vitest";
import { moveCursor, scrollToRow, visibleRange } from "./windowing";

describe("visibleRange", () => {
  it("renders only a window of a 10,000 row list", () => {
    const r = visibleRange({ scrollTop: 0, viewportHeight: 400, rowHeight: 40, count: 10_000 });
    expect(r.start).toBe(0);
    expect(r.end).toBe(10 + 6);
    expect(r.totalHeight).toBe(400_000);
  });
  it("moves the window with the scroll position", () => {
    const r = visibleRange({ scrollTop: 200_000, viewportHeight: 400, rowHeight: 40, count: 10_000, overscan: 5 });
    expect(r.start).toBe(5000 - 5);
    expect(r.end).toBe(5010 + 5);
    expect(r.offsetTop).toBe(r.start * 40);
  });
  it("clamps at the end of the list", () => {
    const r = visibleRange({ scrollTop: 9_999_999, viewportHeight: 400, rowHeight: 40, count: 10_000 });
    expect(r.end).toBe(10_000);
    expect(r.end - r.start).toBeLessThan(30);
  });
  it("handles an empty list and a list shorter than the viewport", () => {
    expect(visibleRange({ scrollTop: 0, viewportHeight: 400, rowHeight: 40, count: 0 })).toEqual({ start: 0, end: 0, offsetTop: 0, totalHeight: 0 });
    const r = visibleRange({ scrollTop: 50, viewportHeight: 400, rowHeight: 40, count: 3 });
    expect([r.start, r.end]).toEqual([0, 3]);
  });
});

describe("scrollToRow", () => {
  it("leaves the scroll alone when the row is visible", () => {
    expect(scrollToRow(3, 0, 400, 40)).toBe(0);
  });
  it("scrolls up and down just enough", () => {
    expect(scrollToRow(1, 200, 400, 40)).toBe(40);
    expect(scrollToRow(20, 0, 400, 40)).toBe(20 * 40 + 40 - 400);
  });
  it("keeps the row clear of a sticky header", () => {
    expect(scrollToRow(20, 0, 400, 40, 36)).toBe(20 * 40 + 40 - 400 + 36);
  });
});

describe("moveCursor", () => {
  it("starts at the first or last row", () => {
    expect(moveCursor(null, 1, 5)).toBe(0);
    expect(moveCursor(null, -1, 5)).toBe(4);
  });
  it("stays inside the list", () => {
    expect(moveCursor(4, 1, 5)).toBe(4);
    expect(moveCursor(0, -1, 5)).toBe(0);
    expect(moveCursor(2, 1, 5)).toBe(3);
  });
  it("has no cursor in an empty list", () => {
    expect(moveCursor(null, 1, 0)).toBeNull();
  });
});
