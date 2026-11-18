// Trace list filters live in the URL, so a view can be shared and survives a reload.
// Primary filters sit in the top row; the rest are behind "More filters".

import { RANGES } from "./hooks";

export const PRIMARY_KEYS = ["q", "range", "error"] as const;
export const MORE_KEYS = ["name", "release", "user_id", "session_id", "min_ms"] as const;
export const DEFAULT_RANGE = "7d";

export type FilterKey = (typeof PRIMARY_KEYS)[number] | (typeof MORE_KEYS)[number];

export const MORE_LABELS: Record<(typeof MORE_KEYS)[number], string> = {
  name: "Name",
  release: "Release",
  user_id: "User",
  session_id: "Session",
  min_ms: "Latency",
};

export function range(params: URLSearchParams): string {
  const r = params.get("range") || DEFAULT_RANGE;
  return r === "all" || r in RANGES ? r : DEFAULT_RANGE;
}

/** Set (or, for an empty value, remove) one filter. Never carries over a stale page. */
export function withParam(params: URLSearchParams, key: FilterKey, value: string): URLSearchParams {
  const next = new URLSearchParams(params);
  const v = value.trim();
  if (v && !(key === "range" && v === DEFAULT_RANGE)) next.set(key, v);
  else next.delete(key);
  next.delete("page");
  return next;
}

export function clearFilters(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const k of [...PRIMARY_KEYS, ...MORE_KEYS]) next.delete(k);
  next.delete("page");
  return next;
}

/** How many of the "More filters" are set (shown as a count on the button). */
export function activeMoreCount(params: URLSearchParams): number {
  return MORE_KEYS.filter((k) => !!params.get(k)).length;
}

export function hasAnyFilter(params: URLSearchParams): boolean {
  return activeMoreCount(params) > 0 || !!params.get("q") || !!params.get("error") || range(params) !== DEFAULT_RANGE;
}

/** Query-string fields for GET /traces (the page window is added by the caller). */
export function traceQuery(params: URLSearchParams, now: number): Record<string, string> {
  const r = range(params);
  return {
    name: params.get("name") || "",
    user_id: params.get("user_id") || "",
    session_id: params.get("session_id") || "",
    release: params.get("release") || "",
    search: params.get("q") || "",
    error: params.get("error") || "",
    min_duration_ms: params.get("min_ms") || "",
    start_after: r === "all" ? "" : String(now - RANGES[r]),
  };
}
