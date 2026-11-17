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
