// Sends Vercel AI SDK-shaped spans (attribute names as `experimental_telemetry` emits them) to a
// Tirkit server with the real OpenTelemetry Node OTLP/HTTP (protobuf) exporter.
// Usage: node export.mjs <endpoint>/v1/traces "<Authorization header>" <base64 png>
import { context, trace } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";

const [url, authorization, png] = process.argv.slice(2);
const exporter = new OTLPTraceExporter({ url, headers: { authorization } });
const provider = new BasicTracerProvider({
  resource: resourceFromAttributes({ "service.name": "node-app" }),
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
const tracer = provider.getTracer("ai");

const prompt = JSON.stringify({ prompt: "What is in this picture?" });
const outer = tracer.startSpan("ai.generateText", {
  attributes: {
    "operation.name": "ai.generateText describe",
    "ai.operationId": "ai.generateText",
    "ai.model.id": "gpt-4o-mini",
    "ai.model.provider": "openai.chat",
    "ai.telemetry.functionId": "describe",
    "ai.telemetry.metadata.sessionId": "sess-node",
    "ai.prompt": prompt,
    "ai.response.text": "A red square.",
    "ai.usage.promptTokens": 50,
    "ai.usage.completionTokens": 4,
  },
});
const ctx = trace.setSpan(context.active(), outer);
const messages = [
  {
    role: "user",
    content: [
      { type: "text", text: "What is in this picture? Mail me at a.b@example.com" },
      { type: "image", image: png, mimeType: "image/png" },
    ],
  },
];
const call = tracer.startSpan(
  "ai.generateText.doGenerate",
  {
    attributes: {
      "operation.name": "ai.generateText.doGenerate describe",
      "ai.operationId": "ai.generateText.doGenerate",
      "ai.model.id": "gpt-4o-mini",
      "ai.model.provider": "openai.chat",
      "ai.prompt.messages": JSON.stringify(messages),
      "ai.response.text": "A red square.",
      "ai.response.finishReason": "stop",
      "ai.response.msToFirstChunk": 120,
      "ai.usage.promptTokens": 50,
      "ai.usage.completionTokens": 4,
      "gen_ai.system": "openai.chat",
      "gen_ai.request.model": "gpt-4o-mini",
    },
  },
  ctx,
);
call.end();
outer.end();
await provider.forceFlush();
await provider.shutdown();
