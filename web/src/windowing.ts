// A very small windowing helper for fixed-height rows: given the scroll position, say which rows
// to render. Rows outside the window are not in the DOM, so a list of 10,000 stays cheap.

export interface WindowInput {
  scrollTop: number;
  viewportHeight: number;
  rowHeight: number;
  count: number;
  /** Extra rows rendered above and below the visible ones, to avoid blank flashes while scrolling. */
  overscan?: number;
}

export interface WindowRange {
  /** First rendered row (inclusive). */
  start: number;
  /** One past the last rendered row. */
  end: number;
  /** Height of the spacer above the first rendered row. */
  offsetTop: number;
  /** Height of the whole list. */
  totalHeight: number;
}

export function visibleRange({ scrollTop, viewportHeight, rowHeight, count, overscan = 6 }: WindowInput): WindowRange {
  const totalHeight = Math.max(0, count) * rowHeight;
  if (count <= 0 || rowHeight <= 0) return { start: 0, end: 0, offsetTop: 0, totalHeight: 0 };
  const top = Math.min(Math.max(0, scrollTop), Math.max(0, totalHeight - viewportHeight));
  const first = Math.floor(top / rowHeight);
  const last = Math.ceil((top + Math.max(0, viewportHeight)) / rowHeight);
  const start = Math.max(0, first - overscan);
  const end = Math.min(count, last + overscan);
  return { start, end, offsetTop: start * rowHeight, totalHeight };
}

/** The scrollTop that brings row `index` fully into view, or the current one if it already is. */
export function scrollToRow(index: number, scrollTop: number, viewportHeight: number, rowHeight: number, headerHeight = 0): number {
  const top = index * rowHeight;
  const bottom = top + rowHeight;
  if (top < scrollTop) return top;
  if (bottom > scrollTop + viewportHeight - headerHeight) return bottom - viewportHeight + headerHeight;
  return scrollTop;
}

/** Move a keyboard cursor by `delta` rows, staying inside 0..count-1 (`null` means no row yet). */
export function moveCursor(current: number | null, delta: number, count: number): number | null {
  if (count <= 0) return null;
  if (current === null) return delta >= 0 ? 0 : count - 1;
  return Math.min(count - 1, Math.max(0, current + delta));
}
