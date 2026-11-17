export const fmtNum = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });

export const fmtCompact = (n: number | null | undefined) =>
  n === null || n === undefined ? "—" : Intl.NumberFormat(undefined, { notation: "compact" }).format(n);

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 1000) return `${ms.toFixed(0)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "—";
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(5)}`;
  return `$${usd.toFixed(usd < 10 ? 3 : 2)}`;
}

export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return `${Math.max(0, Math.floor(s))}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function preview(value: unknown, max = 120): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.length > max ? value.slice(0, max) + "…" : value;
  const text = extractText(value);
  const s = text || JSON.stringify(value);
  return s.length > max ? s.slice(0, max) + "…" : s;
}

/** Best-effort readable text from common input/output shapes. */
const KIND_LABELS: Record<string, string> = {
  speech_to_text: "stt",
  text_to_speech: "tts",
  speech_to_speech: "voice",
  image_generation: "image",
  retrieval: "retrieve",
  embedding: "embed",
  evaluator: "eval",
  guardrail: "guard",
};
export const kindLabel = (kind: string) => KIND_LABELS[kind] ?? kind;

function mediaLabel(v: Record<string, unknown>): string {
  const kb = Math.max(1, Math.round(Number(v.size || 0) / 1024));
  return `[${String(v.mime || "media")}, ${kb} KB]`;
}

export function extractText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object" && !Array.isArray(value) && (value as Record<string, unknown>).type === "media")
    return mediaLabel(value as Record<string, unknown>);
  if (Array.isArray(value)) {
    const last = value[value.length - 1];
    return extractText(last);
  }
  if (typeof value === "object") {
    const v = value as Record<string, unknown>;
    for (const k of ["output_text", "content", "text", "question", "query", "prompt", "answer", "input"]) {
      if (k in v && v[k] !== null && v[k] !== undefined) return extractText(v[k]);
    }
    if (Array.isArray(v.messages)) return extractText(v.messages);
    if (Array.isArray(v.parts)) return v.parts.map((p) => extractText(p)).join(" ");
    const media = Object.values(v).filter(
      (x) => typeof x === "object" && x !== null && (x as Record<string, unknown>).type === "media",
    );
    if (media.length) return media.map((x) => mediaLabel(x as Record<string, unknown>)).join(" ");
  }
  return "";
}
