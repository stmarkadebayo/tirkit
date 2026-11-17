// Unit tests with a fake transport and a fake OpenAI client (no network).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as tirkit from "../dist/index.js";

function capture() {
  const posted = [];
  const fetchImpl = async (url, init) => {
    posted.push({ url, body: init.body ? JSON.parse(init.body) : null, headers: init.headers });
    if (url.includes("/api/v1/prompts/")) {
      return new Response(JSON.stringify({ name: "greet", version: 3, kind: "text", template: "Hi {{name}}", config: {}, labels: ["production"] }), { status: 200 });
    }
    return new Response(JSON.stringify({ accepted: 1, score_id: "s1" }), { status: 200 });
  };
  tirkit.init({ host: "http://tirkit.test", publicKey: "pk", secretKey: "sk", fetch: fetchImpl, flushIntervalMs: 10_000 });
  const spans = () => posted.filter((p) => p.url.endsWith("/api/v1/ingest")).flatMap((p) => p.body.spans);
  return { posted, spans };
}

test("nested spans across awaits share a trace and parent", async () => {
  const { spans, posted } = capture();
  await tirkit.traceContext({ sessionId: "s1", userId: "u1" }, () =>
    tirkit.span("root", { kind: "agent", input: { q: "hi" } }, async () => {
      await Promise.all([1, 2].map((i) => tirkit.span(`child-${i}`, { kind: "tool" }, async () => {
        await new Promise((r) => setTimeout(r, 5 * i));
        return i;
      })));
      return "done";
    }),
  );
  await tirkit.flush();
  const all = spans();
  const root = all.find((s) => s.name === "root");
  const kids = all.filter((s) => s.name.startsWith("child"));
  assert.equal(kids.length, 2);
  for (const k of kids) {
    assert.equal(k.parent_id, root.span_id);
    assert.equal(k.trace_id, root.trace_id);
    assert.equal(k.session_id, "s1");
  }
  assert.equal(root.output, "done");
  assert.match(posted[0].headers.authorization, /^Basic /);
});

test("errors are recorded and rethrown", async () => {
  const { spans } = capture();
  await assert.rejects(tirkit.span("boom", {}, async () => { throw new Error("bad"); }));
  await tirkit.flush();
  const s = spans().find((x) => x.name === "boom");
  assert.equal(s.status, "error");
  assert.match(s.error, /bad/);
});

test("OpenAI chat calls are traced with usage, including streams", async () => {
  const { spans } = capture();
  const fake = {
    baseURL: "http://localhost:11434/v1",
    chat: {
      completions: {
        async create(args) {
          if (args.stream) {
            return {
              async *[Symbol.asyncIterator]() {
                yield { model: "llama3.2", choices: [{ delta: { content: "Hel" } }] };
                yield { model: "llama3.2", choices: [{ delta: { content: "lo" } }] };
                yield { choices: [], usage: { prompt_tokens: 3, completion_tokens: 2 } };
              },
            };
          }
          return { model: "gpt-4o-mini", choices: [{ message: { role: "assistant", content: "ok" }, finish_reason: "stop" }], usage: { prompt_tokens: 10, completion_tokens: 1 } };
        },
      },
    },
  };
  tirkit.instrumentOpenAI(fake);
  await fake.chat.completions.create({ model: "gpt-4o-mini", messages: [{ role: "user", content: "x" }], temperature: 0 });
  let text = "";
  for await (const c of await fake.chat.completions.create({ model: "llama3.2", messages: [], stream: true })) text += c.choices[0]?.delta?.content ?? "";
  assert.equal(text, "Hello");
  await tirkit.flush();
  const [a, b] = spans().filter((s) => s.name === "openai.chat");
  assert.deepEqual(a.usage, { input_tokens: 10, output_tokens: 1 });
  assert.deepEqual(a.model_params, { temperature: 0 });
  assert.equal(a.attributes.provider, "openai-compatible");
  assert.equal(b.output.content, "Hello");
  assert.equal(b.usage.output_tokens, 2);
  assert.ok(b.ttft_ms !== null);
});

test("prompts compile and link spans; feedback posts a score", async () => {
  const { spans, posted } = capture();
  const p = await tirkit.getPrompt("greet", { ttlMs: 0 });
  assert.equal(tirkit.compilePrompt(p, { name: "Ada" }), "Hi Ada");
  assert.throws(() => tirkit.compilePrompt(p, {}), /missing prompt variable/);
  await tirkit.usePrompt(p, () => tirkit.span("call", { kind: "llm" }, async () => "x"));
  await tirkit.flush();
  assert.equal(spans().find((s) => s.name === "call").prompt_version, "greet@3");
  await tirkit.feedback("t1", 1, { comment: "great" });
  const fb = posted.find((x) => x.url.endsWith("/api/v1/scores"));
  assert.equal(fb.body.value, 1);
  assert.equal(fb.body.source, "user_feedback");
});

// Retry (plan A5): a fake server that answers 429 and 503.
function scripted(script, opts = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ t: Date.now(), names: JSON.parse(init.body).spans.map((s) => s.name) });
    const [status, headers] = script.shift() ?? [200, {}];
    return new Response(status === 200 ? '{"accepted":1}' : "{}", { status, headers });
  };
  tirkit.init({ host: "http://tirkit.test", publicKey: "pk", secretKey: "sk", fetch: fetchImpl, flushIntervalMs: 10_000, ...opts });
  return calls;
}

test("429 honours Retry-After", async () => {
  const calls = scripted([[429, { "retry-after": "1" }]]);
  await tirkit.span("a", {}, async () => 1);
  await tirkit.flush();
  assert.equal(calls.length, 2);
  const waited = calls[1].t - calls[0].t;
  assert.ok(waited >= 950 && waited < 2000, `waited ${waited} ms`);
  assert.equal(tirkit.stats().failed, 0);
});

test("503 backs off and then succeeds", async () => {
  const calls = scripted([[503, {}], [503, {}]]);
  await tirkit.span("b", {}, async () => 1);
  await tirkit.flush();
  assert.equal(calls.length, 3);
  assert.equal(tirkit.stats().failed, 0);
});

test("retry time is bounded", async () => {
  const calls = scripted(Array(1000).fill([503, {}]), { maxRetries: 1000, maxRetryMs: 1000 });
  await tirkit.span("c", {}, async () => 1);
  const t0 = Date.now();
  await tirkit.flush();
  assert.ok(Date.now() - t0 < 2500);
  assert.ok(calls.length > 1);
  assert.equal(tirkit.stats().failed, 1);
});

test("Retry-After beyond the budget gives up at once", async () => {
  const calls = scripted([[429, { "retry-after": "3600" }]], { maxRetryMs: 2000 });
  await tirkit.span("d", {}, async () => 1);
  const t0 = Date.now();
  await tirkit.flush();
  assert.ok(Date.now() - t0 < 1000);
  assert.equal(calls.length, 1);
  assert.equal(tirkit.stats().failed, 1);
});

test("client errors are not retried", async () => {
  const calls = scripted([[400, {}]]);
  await tirkit.span("e", {}, async () => 1);
  await tirkit.flush();
  assert.equal(calls.length, 1);
  assert.equal(tirkit.stats().failed, 1);
});

test("parseRetryAfter and jittered backoff", () => {
  assert.equal(tirkit.parseRetryAfter("120"), 120);
  assert.equal(tirkit.parseRetryAfter("soon"), null);
  assert.equal(tirkit.parseRetryAfter(null), null);
  const future = new Date(Date.now() + 30_000).toUTCString();
  const s = tirkit.parseRetryAfter(future);
  assert.ok(s >= 25 && s <= 31, String(s));
  for (let attempt = 0; attempt < 12; attempt++) {
    const ceiling = Math.min(5000, 200 * 2 ** attempt);
    const xs = Array.from({ length: 200 }, () => tirkit.backoffDelayMs(attempt));
    assert.ok(xs.every((x) => x >= 0 && x <= ceiling));
    assert.ok(Math.max(...xs) - Math.min(...xs) > ceiling * 0.5);
  }
});
