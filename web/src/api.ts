// Typed client for the Tirkit REST API. Session cookie auth; state-changing requests send the
// CSRF token from the `tirkit_csrf` cookie.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface User {
  user_id: string;
  email: string;
  name: string | null;
  is_admin: boolean;
}
export interface ProjectRef {
  project_id: string;
  name: string;
  role: "admin" | "member" | "viewer";
}
export interface Me {
  user: User;
  csrf: string;
  projects: ProjectRef[];
}
export interface Trace {
  trace_id: string;
  name: string | null;
  session_id: string | null;
  user_id: string | null;
  release: string | null;
  tags: string[];
  input: Json;
  output: Json;
  start: number | null;
  end: number | null;
  duration_ms: number | null;
  n_spans: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  has_error: boolean;
}
export interface Span {
  name: string;
  trace_id: string;
  span_id: string;
  parent_id: string | null;
  start: number;
  end: number | null;
  kind: string;
  status: string;
  level: string;
  model: string | null;
  model_params: Record<string, Json>;
  input: Json;
  output: Json;
  usage: Record<string, number>;
  cost_usd: number | null;
  error: string | null;
  session_id: string | null;
  user_id: string | null;
  release: string | null;
  tags: string[];
  prompt_version: string | null;
  ttft_ms: number | null;
  attributes: Record<string, Json>;
}
export interface Score {
  score_id: string;
  trace_id: string | null;
  span_id: string | null;
  experiment_id: string | null;
  item_idx: number | null;
  name: string;
  value: number | null;
  value_str: string | null;
  source: string;
  scorer_version: string | null;
  reason: string | null;
  error: string | null;
  author: string | null;
  created: number;
}
export interface TraceDetail {
  trace: Trace;
  spans: Span[];
  scores: Score[];
}
export interface MetricPoint {
  bucket: number;
  t: number;
  spans: number;
  errors: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  traces?: number;
  trace_errors?: number;
  p50_ms?: number;
  p95_ms?: number;
  p99_ms?: number;
}
export interface Metrics {
  since: number;
  until: number;
  bucket_s: number;
  group_by: string | null;
  series: Record<string, MetricPoint[]>;
  scores: Record<string, { bucket: number; t: number; mean: number; n: number }[]>;
  latency_sample_size: number;
  latency_sample_capped: boolean;
}
export interface Filters {
  names: string[];
  span_names: string[];
  models: string[];
  releases: string[];
  kinds: string[];
  scores: string[];
}
export interface DatasetSummary {
  name: string;
  description: string | null;
  created: number;
  n_versions: number;
  latest_version: string;
  n_items: number;
}
export interface Example {
  input: Record<string, Json>;
  expected: Json;
  metadata: Record<string, Json>;
}
export interface DatasetDetail {
  name: string;
  version: string;
  items: Example[];
  versions: { version: string; created: number; n_items: number; note: string | null }[];
}
export interface ScorerSummary {
  n: number;
  failed: number;
  mean?: number;
  ci?: [number, number];
}
export interface ExperimentSummary {
  experiment_id: string;
  name: string | null;
  dataset: string;
  version: string;
  target: string | null;
  status: string;
  created: number;
  finished: number | null;
  n_items?: number;
  summary?: Record<string, ScorerSummary>;
}
export interface Comparison {
  scorer: string;
  n: number;
  excluded: number;
  mean_baseline: number;
  mean_candidate: number;
  ci_baseline: [number, number];
  ci_candidate: [number, number];
  diff: number;
  diff_ci: [number, number];
  higher_is_better: boolean;
  verdict: string;
}
export interface CompareItem {
  idx: number;
  example: Example | null;
  baseline: { output: Json; trace_id: string | null; scores: Record<string, number | null> };
  candidate: { output: Json; trace_id: string | null; scores: Record<string, number | null> };
}
export interface CompareResult {
  baseline: string;
  candidate: string;
  comparisons: Comparison[];
  markdown: string;
  items: CompareItem[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function csrf(): string {
  const m = document.cookie.match(/(?:^|; )tirkit_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  const method = (init.method || "GET").toUpperCase();
  if (method !== "GET") headers["X-CSRF-Token"] = csrf();
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const res = await fetch(path, { ...init, method, headers, body, credentials: "same-origin" });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const data = await res.json();
      msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  const type = res.headers.get("content-type") || "";
  return (type.includes("json") ? res.json() : res.text()) as Promise<T>;
}

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const projectPath = (pid: string, rest = "") => `/api/projects/${encodeURIComponent(pid)}${rest}`;
export const mediaUrl = (pid: string, sha: string) => projectPath(pid, `/media/${sha}`);
