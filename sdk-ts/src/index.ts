/**
 * Tirkit SDK for Node.js: the same concepts as the Python SDK (spans, trace context, feedback,
 * prompts, OpenAI auto-instrumentation), sending to a Tirkit server.
 *
 *   import { init, span, instrumentOpenAI, feedback, getPrompt } from "@tirkit/sdk";
 *   init();                         // TIRKIT_HOST, TIRKIT_PUBLIC_KEY, TIRKIT_SECRET_KEY
 *   instrumentOpenAI(openaiClient); // traces chat.completions, embeddings, responses
 *   await span("answer", { kind: "agent", input: q }, async (s) => { ... s.output = text; });
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash, randomBytes } from "node:crypto";

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface SpanData {
  name: string;
  trace_id: string;
  span_id: string;
  parent_id: string | null;
  start: number;
  end: number | null;
  kind: string;
  status: "ok" | "error";
  level: string;
  model: string | null;
  model_params: Record<string, unknown>;
  input: unknown;
  output: unknown;
  usage: Record<string, number>;
  cost_usd: number | null;
  error: string | null;
  session_id: string | null;
  user_id: string | null;
  release: string | null;
  tags: string[];
  prompt_version: string | null;
  ttft_ms: number | null;
  attributes: Record<string, unknown>;
}

export interface SpanOptions {
  kind?: string;
  input?: unknown;
  model?: string;
  modelParams?: Record<string, unknown>;
  attributes?: Record<string, unknown>;
  sessionId?: string;
  userId?: string;
  release?: string;
  tags?: string[];
}

export interface InitOptions {
  host?: string;
  publicKey?: string;
  secretKey?: string;
  batchSize?: number;
  flushIntervalMs?: number;
  maxQueue?: number;
  /** Replace the HTTP transport (tests). */
  fetch?: typeof fetch;
}

interface Ctx {
  span: SpanData | null;
  trace: { sessionId?: string; userId?: string; release?: string; tags?: string[]; promptVersion?: string };
}

const storage = new AsyncLocalStorage<Ctx>();
const hex = (n: number) => randomBytes(n).toString("hex");
const now = () => Date.now() / 1000;

class Exporter {
  queue: SpanData[] = [];
  dropped = 0;
  exported = 0;
  failed = 0;
  private timer: NodeJS.Timeout | null = null;
  private inflight: Promise<void> = Promise.resolve();

  constructor(
    private host: string,
    private auth: string,
    private opts: Required<Pick<InitOptions, "batchSize" | "flushIntervalMs" | "maxQueue">>,
    private fetchImpl: typeof fetch,
  ) {
    this.timer = setInterval(() => void this.flush(), opts.flushIntervalMs);
    this.timer.unref();
  }

  push(s: SpanData) {
    if (this.queue.length >= this.opts.maxQueue) {
      this.dropped++;
      return;
    }
    this.queue.push(s);
    if (this.queue.length >= this.opts.batchSize) void this.flush();
  }

  flush(): Promise<void> {
    this.inflight = this.inflight.then(async () => {
      while (this.queue.length) {
        const batch = this.queue.splice(0, this.opts.batchSize);
        let delay = 200;
        for (let attempt = 0; attempt < 4; attempt++) {
          try {
            const res = await this.fetchImpl(`${this.host}/api/v1/ingest`, {
              method: "POST",
              headers: { "content-type": "application/json", authorization: this.auth },
              body: safeStringify({ spans: batch }),
            });
            if (!res.ok) throw new Error(`ingest returned ${res.status}`);
            this.exported += batch.length;
            break;
          } catch (err) {
            if (attempt === 3) {
              this.failed += batch.length;
              console.warn(`tirkit: failed to export ${batch.length} spans: ${err}`);
            } else {
              await new Promise((r) => setTimeout(r, delay));
              delay *= 2;
            }
          }
        }
      }
    });
    return this.inflight;
  }

  async shutdown() {
    if (this.timer) clearInterval(this.timer);
    await this.flush();
  }
}

let client: { host: string; auth: string; exporter: Exporter; fetch: typeof fetch } | null = null;

/** Start sending spans to a Tirkit server. Reads TIRKIT_HOST / TIRKIT_PUBLIC_KEY / TIRKIT_SECRET_KEY. */
export function init(opts: InitOptions = {}) {
  const host = (opts.host ?? process.env.TIRKIT_HOST ?? "").replace(/\/$/, "");
  const pk = opts.publicKey ?? process.env.TIRKIT_PUBLIC_KEY ?? "";
  const sk = opts.secretKey ?? process.env.TIRKIT_SECRET_KEY ?? "";
  if (!host || !pk || !sk) throw new Error("tirkit: host, publicKey and secretKey are required");
  const auth = "Basic " + Buffer.from(`${pk}:${sk}`).toString("base64");
  const f = opts.fetch ?? fetch;
  client = {
    host,
    auth,
    fetch: f,
    exporter: new Exporter(host, auth, { batchSize: opts.batchSize ?? 100, flushIntervalMs: opts.flushIntervalMs ?? 500, maxQueue: opts.maxQueue ?? 10_000 }, f),
  };
  return client;
}

export const flush = () => client?.exporter.flush() ?? Promise.resolve();
export const shutdown = () => client?.exporter.shutdown() ?? Promise.resolve();
export const stats = () => (client ? { queued: client.exporter.queue.length, exported: client.exporter.exported, dropped: client.exporter.dropped, failed: client.exporter.failed } : null);

function safeStringify(v: unknown): string {
  const seen = new WeakSet();
  return JSON.stringify(v, (_k, val) => {
    if (typeof val === "bigint") return val.toString();
    if (val instanceof Uint8Array) return { type: "bytes", size: val.byteLength };
    if (typeof val === "object" && val !== null) {
      if (seen.has(val)) return "[circular]";
      seen.add(val);
    }
    return val;
  });
}

function snapshot(v: unknown): unknown {
  if (v === undefined) return null;
  try {
    return JSON.parse(safeStringify(v));
  } catch {
    return String(v);
  }
}

export function currentSpan(): SpanData | null {
  return storage.getStore()?.span ?? null;
}

/** Start a span that is not made current (for streams). Finish it with {@link finishSpan}. */
export function startSpan(name: string, opts: SpanOptions = {}): SpanData {
  const ctx = storage.getStore();
  const parent = ctx?.span ?? null;
  const t = ctx?.trace ?? {};
  return {
    name,
    trace_id: parent?.trace_id ?? hex(16),
    span_id: hex(8),
    parent_id: parent?.span_id ?? null,
    start: now(),
    end: null,
    kind: opts.kind ?? "generic",
    status: "ok",
    level: "default",
    model: opts.model ?? null,
    model_params: (snapshot(opts.modelParams ?? {}) as Record<string, unknown>) ?? {},
    input: snapshot(opts.input),
    output: null,
    usage: {},
    cost_usd: null,
    error: null,
    session_id: opts.sessionId ?? t.sessionId ?? parent?.session_id ?? null,
    user_id: opts.userId ?? t.userId ?? parent?.user_id ?? null,
    release: opts.release ?? t.release ?? parent?.release ?? null,
    tags: [...new Set([...(parent?.tags ?? []), ...(t.tags ?? []), ...(opts.tags ?? [])])],
    prompt_version: t.promptVersion ?? null,
    ttft_ms: null,
    attributes: { ...(opts.attributes ?? {}) },
  };
}

export function finishSpan(s: SpanData) {
  try {
    if (s.end === null) s.end = now();
    s.output = snapshot(s.output);
    client?.exporter.push(s);
  } catch (err) {
    console.warn(`tirkit: could not record span ${s.name}: ${err}`);
  }
}

export function recordError(s: SpanData, err: unknown) {
  s.status = "error";
  s.level = "error";
  s.error = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}

/** Run `fn` inside a span. Nested calls (including across awaits) become children. */
export async function span<T>(name: string, opts: SpanOptions, fn: (s: SpanData) => Promise<T> | T): Promise<T> {
  const s = startSpan(name, opts);
  const ctx = storage.getStore();
  try {
    const result = await storage.run({ span: s, trace: ctx?.trace ?? {} }, () => fn(s));
    if (s.output === null && result !== undefined) s.output = result;
    return result;
  } catch (err) {
    recordError(s, err);
    throw err;
  } finally {
    finishSpan(s);
  }
}

/** Wrap a function so every call is a span (input = arguments, output = return value). */
export function traced<A extends unknown[], R>(name: string, fn: (...args: A) => Promise<R> | R, opts: SpanOptions = {}) {
  return (...args: A): Promise<R> => span(name, { ...opts, input: args.length === 1 ? args[0] : args }, () => fn(...args));
}

/** Set session, user, release or tags for every span started inside `fn`. */
export function traceContext<T>(ctx: Ctx["trace"], fn: () => T): T {
  const cur = storage.getStore();
  return storage.run({ span: cur?.span ?? null, trace: { ...(cur?.trace ?? {}), ...ctx } }, fn);
}

/** Record end-user feedback on a trace (1/0 for thumbs, a rating, or a category). */
export async function feedback(traceId: string, value: number | null, opts: { name?: string; comment?: string; valueStr?: string } = {}) {
  if (!client) throw new Error("tirkit: call init() first");
  const res = await client.fetch(`${client.host}/api/v1/scores`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: client.auth },
    body: JSON.stringify({ trace_id: traceId, name: opts.name ?? "user_feedback", value, value_str: opts.valueStr ?? null, comment: opts.comment ?? null, source: "user_feedback" }),
  });
  if (!res.ok) throw new Error(`tirkit: feedback failed (${res.status})`);
  return ((await res.json()) as { score_id: string }).score_id;
}

export interface Prompt {
  name: string;
  version: number;
  kind: "text" | "chat";
  template: string | { role: string; content: string }[];
  config: Record<string, unknown>;
  labels: string[];
}

const promptCache = new Map<string, { at: number; prompt: Prompt }>();
const VAR = /\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}/g;

function render(text: string, vars: Record<string, unknown>): string {
  return text.replace(VAR, (_m, path: string) => {
    let cur: unknown = vars;
    for (const part of path.split(".")) {
      if (cur && typeof cur === "object" && part in (cur as object)) cur = (cur as Record<string, unknown>)[part];
      else throw new Error(`tirkit: missing prompt variable ${path}`);
    }
    return String(cur);
  });
}

export function compilePrompt(p: Prompt, vars: Record<string, unknown>) {
  if (p.kind === "text") return render(String(p.template), vars);
  return (p.template as { role: string; content: string }[]).map((m) => ({ ...m, content: typeof m.content === "string" ? render(m.content, vars) : m.content }));
}

/** Fetch a prompt by label (default production, else latest) or version; cached for `ttlMs`. */
export async function getPrompt(name: string, opts: { label?: string; version?: number; ttlMs?: number } = {}): Promise<Prompt> {
  if (!client) throw new Error("tirkit: call init() first");
  const key = `${name}|${opts.version ?? opts.label ?? "production"}`;
  const hit = promptCache.get(key);
  if (hit && Date.now() - hit.at < (opts.ttlMs ?? 60_000)) return hit.prompt;
  const params = new URLSearchParams();
  if (opts.label) params.set("label", opts.label);
  if (opts.version) params.set("version", String(opts.version));
  try {
    const res = await client.fetch(`${client.host}/api/v1/prompts/${encodeURIComponent(name)}?${params}`, { headers: { authorization: client.auth } });
    if (!res.ok) throw new Error(`tirkit: prompt ${name} not found (${res.status})`);
    const prompt = (await res.json()) as Prompt;
    promptCache.set(key, { at: Date.now(), prompt });
    return prompt;
  } catch (err) {
    if (hit) return hit.prompt; // serve stale when the server is unreachable
    throw err;
  }
}

/** Spans started inside `fn` record the prompt version that produced them. */
export function usePrompt<T>(p: Prompt, fn: () => T): T {
  return traceContext({ promptVersion: `${p.name}@${p.version}` }, fn);
}

// -- OpenAI auto-instrumentation ------------------------------------------------------------------

type AnyFn = (...args: any[]) => any; // eslint-disable-line @typescript-eslint/no-explicit-any

function chatUsage(s: SpanData, u: any) { // eslint-disable-line @typescript-eslint/no-explicit-any
  if (!u) return;
  const set = (k: string, v: unknown) => {
    if (typeof v === "number") s.usage[k] = v;
  };
  set("input_tokens", u.prompt_tokens ?? u.input_tokens);
  set("output_tokens", u.completion_tokens ?? u.output_tokens);
  set("cached_input_tokens", u.prompt_tokens_details?.cached_tokens ?? u.input_tokens_details?.cached_tokens);
  set("input_audio_tokens", u.prompt_tokens_details?.audio_tokens);
  set("output_audio_tokens", u.completion_tokens_details?.audio_tokens);
}

function provider(clientObj: any): Record<string, unknown> { // eslint-disable-line @typescript-eslint/no-explicit-any
  const base = String(clientObj?.baseURL ?? clientObj?._client?.baseURL ?? "");
  try {
    const host = base ? new URL(base).hostname : "api.openai.com";
    return host === "api.openai.com" ? { provider: "openai" } : { provider: "openai-compatible", provider_host: host };
  } catch {
    return { provider: "openai" };
  }
}

function wrapStream(stream: any, s: SpanData) { // eslint-disable-line @typescript-eslint/no-explicit-any
  const iter = stream[Symbol.asyncIterator].bind(stream);
  let text = "";
  const tools: Record<number, { id?: string; name?: string; arguments: string }> = {};
  stream[Symbol.asyncIterator] = async function* () {
    let first = true;
    try {
      for await (const chunk of iter()) {
        if (first) {
          s.ttft_ms = (now() - s.start) * 1000;
          first = false;
        }
        if (chunk?.model) s.model = chunk.model;
        if (chunk?.usage) chatUsage(s, chunk.usage);
        const d = chunk?.choices?.[0]?.delta;
        if (d?.content) text += d.content;
        for (const tc of d?.tool_calls ?? []) {
          const slot = (tools[tc.index ?? 0] ??= { arguments: "" });
          slot.id = tc.id ?? slot.id;
          slot.name = tc.function?.name ?? slot.name;
          slot.arguments += tc.function?.arguments ?? "";
        }
        if (chunk?.type === "response.output_text.delta") text += chunk.delta ?? "";
        if (chunk?.type === "response.completed") chatUsage(s, chunk.response?.usage);
        yield chunk;
      }
    } catch (err) {
      recordError(s, err);
      throw err;
    } finally {
      const calls = Object.values(tools);
      s.output = { role: "assistant", content: text, ...(calls.length ? { tool_calls: calls } : {}) };
      finishSpan(s);
    }
  };
  return stream;
}

function wrapMethod(owner: any, method: string, name: string, kind: string, build: (args: any, res: any, s: SpanData) => void, client: any) { // eslint-disable-line @typescript-eslint/no-explicit-any
  if (!owner || typeof owner[method] !== "function" || owner[method].__tirkit) return;
  const original: AnyFn = owner[method].bind(owner);
  const wrapped = async (args: any, ...rest: any[]) => { // eslint-disable-line @typescript-eslint/no-explicit-any
    const { messages, input, model, tools, stream, ...params } = args ?? {};
    const s = startSpan(name, {
      kind,
      model,
      input: messages ? { messages, ...(tools ? { tools } : {}) } : { input },
      modelParams: params,
      attributes: provider(client),
    });
    let res;
    try {
      res = await original(args, ...rest);
    } catch (err) {
      recordError(s, err);
      finishSpan(s);
      throw err;
    }
    if (stream && res && typeof res[Symbol.asyncIterator] === "function") return wrapStream(res, s);
    try {
      build(args, res, s);
    } catch {
      /* tracing must never break the caller */
    }
    finishSpan(s);
    return res;
  };
  (wrapped as any).__tirkit = true; // eslint-disable-line @typescript-eslint/no-explicit-any
  owner[method] = wrapped;
}

/** Trace calls made with an OpenAI client instance (and any OpenAI-compatible server). */
export function instrumentOpenAI<T extends object>(openai: T): T {
  const c = openai as any; // eslint-disable-line @typescript-eslint/no-explicit-any
  wrapMethod(c.chat?.completions, "create", "openai.chat", "llm", (_a, res, s) => {
    s.model = res.model ?? s.model;
    const msgs = (res.choices ?? []).map((ch: any) => ch.message); // eslint-disable-line @typescript-eslint/no-explicit-any
    s.output = msgs.length === 1 ? msgs[0] : msgs;
    s.attributes.finish_reason = (res.choices ?? []).map((ch: any) => ch.finish_reason); // eslint-disable-line @typescript-eslint/no-explicit-any
    chatUsage(s, res.usage);
  }, c);
  wrapMethod(c.responses, "create", "openai.responses", "llm", (_a, res, s) => {
    s.model = res.model ?? s.model;
    s.output = { output_text: res.output_text, output: res.output };
    chatUsage(s, res.usage);
  }, c);
  wrapMethod(c.embeddings, "create", "openai.embeddings", "embedding", (_a, res, s) => {
    s.output = { count: res.data?.length ?? 0, dimensions: res.data?.[0]?.embedding?.length ?? null };
    chatUsage(s, res.usage);
  }, c);
  return openai;
}

/** Content hash used for media references (same as the Python SDK). */
export const sha256 = (data: Uint8Array) => createHash("sha256").update(data).digest("hex");
