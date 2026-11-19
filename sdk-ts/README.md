# @tirkit/sdk

Tracing for Node.js (18+) apps with [Tirkit](https://github.com/stmarkadebayo/tirkit), the open-source
LLM observability and evaluation platform. No runtime dependencies.

```bash
npm install @tirkit/sdk
```

You need a Tirkit server to send traces to. The quickest is `pip install "tirkit[server]" && tirkit serve`
(see the [quickstart](https://github.com/stmarkadebayo/tirkit/blob/main/docs/site/quickstart.md));
create an API key in the UI and set:

```bash
export TIRKIT_HOST=http://localhost:8000 TIRKIT_PUBLIC_KEY=pk-tirkit-... TIRKIT_SECRET_KEY=sk-tirkit-...
```

```ts
import OpenAI from "openai";
import { init, span, instrumentOpenAI, shutdown } from "@tirkit/sdk";

init(); // reads the three variables above; makes no network call
const openai = instrumentOpenAI(new OpenAI()); // model calls are traced, with tokens, cost and latency

const answer = await span("answer", { kind: "agent", input: "What is the capital of Kenya?" }, async (s) => {
  const reply = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "What is the capital of Kenya?" }],
  });
  s.output = reply.choices[0].message.content;
  return s.output;
});

await shutdown(); // flushes queued spans before the process exits
```

- **Safe to add to a running app.** Tracing never throws into your code and never blocks it. If the
  server is down, spans are retried with backoff and then dropped, with one log line.
- **Nesting** follows `await` (`AsyncLocalStorage`); `traced(name, fn)` wraps a function,
  `traceContext({ userId, sessionId }, fn)` tags everything inside it.
- **Also:** `feedback(traceId, 1)`, versioned prompts (`getPrompt`, `usePrompt`), streaming with
  time to first token.

Full reference: [TypeScript SDK docs](https://github.com/stmarkadebayo/tirkit/blob/main/docs/site/typescript-sdk.md).
Other languages can send OpenTelemetry instead. Licence: Apache-2.0.
