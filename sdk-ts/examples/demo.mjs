// Node demo: a tiny "agent" traced into a Tirkit server.
//   TIRKIT_HOST=http://localhost:8000 TIRKIT_PUBLIC_KEY=... TIRKIT_SECRET_KEY=... node examples/demo.mjs
import * as tirkit from "../dist/index.js";

tirkit.init();

const retrieve = tirkit.traced("retrieve", async (q) => ["Standard shipping takes 3 to 5 business days."], { kind: "retrieval" });

async function answer(question, i) {
  return tirkit.traceContext({ sessionId: `node-session-${i % 2}`, userId: "node-user", release: "node-demo" }, () =>
    tirkit.span("node-agent", { kind: "agent", input: { question } }, async (s) => {
      const docs = await retrieve(question);
      const reply = await tirkit.span("llm", { kind: "llm", model: "gpt-4o-mini", input: { messages: [{ role: "user", content: question }] } }, async (llm) => {
        llm.usage = { input_tokens: 42, output_tokens: 9 };
        return docs[0];
      });
      s.output = reply;
      return { traceId: s.trace_id, reply };
    }),
  );
}

const results = [];
for (let i = 0; i < 3; i++) results.push(await answer(`How long does shipping take? (${i})`, i));
await tirkit.feedback(results[0].traceId, 1, { comment: "from node" });
await tirkit.shutdown();
console.log(JSON.stringify({ sent: results.length, stats: tirkit.stats() }));
