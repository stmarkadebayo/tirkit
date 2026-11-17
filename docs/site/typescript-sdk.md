# TypeScript SDK

`sdk-ts/` is a zero-dependency Node.js SDK (Node 18+).

```ts
import OpenAI from "openai";
import { init, span, traced, traceContext, instrumentOpenAI, feedback, getPrompt, compilePrompt, usePrompt, shutdown } from "@tirkit/sdk";

init(); // TIRKIT_HOST, TIRKIT_PUBLIC_KEY, TIRKIT_SECRET_KEY
const openai = instrumentOpenAI(new OpenAI());

const prompt = await getPrompt("support", { label: "production" });
const result = await traceContext({ userId: "u-1", sessionId: "chat-9" }, () =>
  span("answer", { kind: "agent", input: { question } }, () =>
    usePrompt(prompt, () => openai.chat.completions.create({ model: "gpt-4o-mini", messages: compilePrompt(prompt, { question }) })),
  ),
);
await feedback(traceId, 1, { comment: "thumbs up" });
await shutdown();
```

Spans nest across `await` through `AsyncLocalStorage`. Streams are traced until they end, with
time to first token. Any other language can send OpenTelemetry instead (see Integrations).
