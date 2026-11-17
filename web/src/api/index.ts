// Client for the Tirkit REST API. Session cookie auth; state-changing requests send the
// CSRF token from the `tirkit_csrf` cookie.
//
// Types come from the server's OpenAPI document (docs/api/openapi.json) via `npm run api`
// (openapi-typescript -> ./schema.ts). Do not hand-write response types here: change the server's
// models (src/tirkit/server/models.py), regenerate, and let `tsc` show what the UI must update.

import type { components } from "./schema";

type Schemas = components["schemas"];

export type Json = Schemas["Json"];
export type User = Schemas["User"];
export type ProjectRef = Schemas["ProjectRef"];
export type Me = Schemas["Me"];
export type Project = Schemas["Project"];
export type Member = Schemas["Member"];
export type ApiKeyInfo = Schemas["ApiKeyInfo"];
export type Trace = Schemas["Trace"];
export type Span = Schemas["Span"];
export type Score = Schemas["ScoreRow"];
export type TraceList = Schemas["TraceList"];
export type TraceDetail = Schemas["TraceDetail"];
export type SessionRow = Schemas["SessionRow"];
export type SessionDetail = Schemas["SessionDetail"];
export type UserRow = Schemas["UserRow"];
export type MetricPoint = Schemas["MetricPoint"];
export type Metrics = Schemas["Metrics"];
export type Filters = Schemas["Filters"];
export type DatasetSummary = Schemas["DatasetSummary"];
export type DatasetSaved = Schemas["DatasetSaved"];
export type Example = Schemas["Example"];
export type DatasetDetail = Schemas["DatasetDetail"];
export type ScorerSummary = Schemas["ScorerSummary"];
export type ExperimentSummary = Schemas["ExperimentSummary"];
export type ExperimentDetail = Schemas["ExperimentDetail"];
export type Comparison = Schemas["Comparison"];
export type CompareItem = Schemas["CompareItem"];
export type CompareResult = Schemas["CompareResult"];
export type PromptSummary = Schemas["PromptSummary"];
export type PromptVersion = Schemas["PromptVersion"];
export type PromptVersions = Schemas["PromptVersions"];
export type PlaygroundModels = Schemas["PlaygroundModels"];
export type PlaygroundRun = Schemas["PlaygroundRun"];
export type Job = Schemas["Job"];
export type ScoreConfig = Schemas["ScoreConfig"];
export type Queue = Schemas["Queue"];
export type NextItem = Schemas["NextItem"];
export type Evaluator = Schemas["Evaluator"];
export type EvaluatorList = Schemas["EvaluatorList"];
export type Alert = Schemas["Alert"];
export type AlertList = Schemas["AlertList"];
export type JudgeReport = Schemas["JudgeReport"];
export type JudgeReports = Schemas["JudgeReports"];
export type AuditEntry = Schemas["AuditEntry"];
export type AuditPage = Schemas["AuditPage"];

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
