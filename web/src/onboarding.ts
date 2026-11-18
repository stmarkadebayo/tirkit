// Logic for the first-run checklist and the copy-paste setup snippets. Pure functions, so the
// state and the text can be tested; the components only fetch data and draw it.

export const LABELS_GOAL = 20;
export const LABELS_FOR_JUDGE = 100;

export interface Progress {
  /** null when the viewer cannot list keys (not an admin) */
  hasKey: boolean | null;
  traceCount: number;
  datasets: number;
  experiments: number;
  labels: number;
  judgeReports: number;
}

export interface Step {
  id: "key" | "trace" | "dataset" | "experiment" | "labels" | "judge";
  title: string;
  detail: string;
  done: boolean;
  /** Page under /p/:pid/ that does the step. */
  to: string;
  cta: string;
}

export function buildChecklist(p: Progress): Step[] {
  const gotTrace = p.traceCount > 0;
  return [
    {
      id: "key",
      title: "Create an API key",
      detail: "Keys let your app send traces to this project.",
      done: p.hasKey === true || gotTrace,
      to: "settings",
      cta: "Create a key",
    },
    {
      id: "trace",
      title: "Send your first trace",
      detail: "Add three lines to your app. It appears here within seconds.",
      done: gotTrace,
      to: "overview",
      cta: "Show the code",
    },
    {
      id: "dataset",
      title: "Build a dataset from traces",
      detail: "Turn real traffic into a test set. Mined examples are de-duplicated and PII-redacted.",
      done: p.datasets > 0,
      to: "datasets",
      cta: "Build a dataset",
    },
    {
      id: "experiment",
      title: "Run an experiment",
      detail: "Score a prompt or model over the dataset and compare it with the last one.",
      done: p.experiments > 0,
      to: "experiments",
      cta: "Run an experiment",
    },
    {
      id: "labels",
      title: `Label ${LABELS_GOAL} items`,
      detail: `${Math.min(p.labels, LABELS_GOAL)} of ${LABELS_GOAL} labelled. Your labels are the ground truth the judge is checked against.`,
      done: p.labels >= LABELS_GOAL,
      to: "annotation",
      cta: "Label items",
    },
    {
      id: "judge",
      title: "Check judge reliability",
      detail: "Measure how often the LLM judge agrees with you before you trust its scores.",
      done: p.judgeReports > 0,
      to: "judge",
      cta: "Check the judge",
    },
  ];
}

export const stepsDone = (steps: Step[]) => steps.filter((s) => s.done).length;
export const allDone = (steps: Step[]) => steps.every((s) => s.done);
/** The first step that is not done, or null. */
export const nextStep = (steps: Step[]): Step | null => steps.find((s) => !s.done) ?? null;

export type SnippetTab = "python" | "typescript" | "otel";
export const SNIPPET_TABS: { id: SnippetTab; label: string }[] = [
  { id: "python", label: "Python" },
  { id: "typescript", label: "TypeScript" },
  { id: "otel", label: "OpenTelemetry" },
];

export interface SnippetInput {
  host: string;
  /** The project's public key, when it has one. */
  publicKey: string | null;
  /** Only passed right after creating a key, the one moment the server shows it. */
  secretKey: string | null;
}

export const SECRET_PLACEHOLDER = "<secret key>";

function envBlock({ host, publicKey, secretKey }: SnippetInput): string {
  return [
    `export TIRKIT_HOST=${host}`,
    `export TIRKIT_PUBLIC_KEY=${publicKey ?? "<public key>"}`,
    `export TIRKIT_SECRET_KEY=${secretKey ?? SECRET_PLACEHOLDER}`,
  ].join("\n");
}

/** The code to copy for one tab. Includes the secret key only if `secretKey` was given. */
export function snippet(tab: SnippetTab, input: SnippetInput): string {
  const env = envBlock(input);
  if (tab === "python") {
    return `pip install tirkit
${env}

# app.py
import tirkit
tirkit.init()        # reads the three TIRKIT_ variables
tirkit.instrument()  # traces OpenAI and Anthropic calls automatically

with tirkit.span("my-step", kind="agent", input="hello") as s:
    s.output = "world"
tirkit.flush()`;
  }
  if (tab === "typescript") {
    return `npm install @tirkit/sdk
${env}

// app.ts
import { init, span, shutdown } from "@tirkit/sdk";
init(); // reads the three TIRKIT_ variables
await span("my-step", { kind: "agent", input: "hello" }, async () => "world");
await shutdown();`;
  }
  return `${env}
export TIRKIT_BASIC_AUTH=$(printf '%s:%s' "$TIRKIT_PUBLIC_KEY" "$TIRKIT_SECRET_KEY" | base64)

export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=${input.host}/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic%20$TIRKIT_BASIC_AUTH"
# then run your OpenTelemetry-instrumented app as usual`;
}

/** The note under the snippet about the secret key. */
export function secretNote(input: SnippetInput): string {
  return input.secretKey
    ? "This secret key is shown once. Copy it now; Tirkit stores only a hash."
    : "Tirkit shows a secret key only when it is created, so it is not filled in. Paste it in place of <secret key>, or create a new key.";
}

const DISMISS_PREFIX = "tirkit.checklist.dismissed.";
export function isDismissed(pid: string): boolean {
  try {
    return localStorage.getItem(DISMISS_PREFIX + pid) === "1";
  } catch {
    return false;
  }
}
export function setDismissed(pid: string, on: boolean): void {
  try {
    if (on) localStorage.setItem(DISMISS_PREFIX + pid, "1");
    else localStorage.removeItem(DISMISS_PREFIX + pid);
  } catch {
    /* storage can be blocked; the checklist then simply comes back on reload */
  }
}
