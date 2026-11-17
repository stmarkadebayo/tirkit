import type { Json } from "../api";
import { mediaUrl } from "../api";

type Obj = Record<string, Json>;

const isObj = (v: Json): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isMedia = (v: Json): v is Obj => isObj(v) && v.type === "media" && typeof v.sha256 === "string";

export function Media({ value, pid }: { value: Obj; pid: string }) {
  const mime = String(value.mime || "");
  const url = mediaUrl(pid, String(value.sha256));
  if (mime.startsWith("image/")) return <img className="media-img" src={url} alt="media" loading="lazy" />;
  if (mime.startsWith("audio/")) return <audio controls preload="none" src={url} style={{ width: "100%" }} />;
  if (mime.startsWith("video/")) return <video controls preload="none" src={url} style={{ maxWidth: "100%" }} />;
  return (
    <a href={url} target="_blank" rel="noreferrer">
      {mime || "file"} ({Math.round(Number(value.size || 0) / 1024)} KB)
    </a>
  );
}

/** Renders one content part (OpenAI, Anthropic and OpenTelemetry GenAI shapes). */
function Part({ part, pid }: { part: Json; pid: string }) {
  if (typeof part === "string") return <pre>{part}</pre>;
  if (isMedia(part)) return <Media value={part} pid={pid} />;
  if (!isObj(part)) return <JsonBlock value={part} />;
  const t = part.type;
  if ((t === "text" || t === "output_text" || t === "input_text") && typeof (part.text ?? part.content) === "string")
    return <pre>{String(part.text ?? part.content)}</pre>;
  if (t === "image_url" && isObj(part.image_url)) {
    const url = part.image_url.url;
    if (isMedia(url as Json)) return <Media value={url as Obj} pid={pid} />;
    if (typeof url === "string") return <a href={url} target="_blank" rel="noreferrer">{url.slice(0, 80)}</a>;
  }
  if (t === "image" && isObj(part.source)) {
    if (isMedia(part.source)) return <Media value={part.source} pid={pid} />;
    const src = part.source as Obj;
    if (typeof src.url === "string") return <img className="media-img" src={src.url} alt="" />;
  }
  if (t === "input_audio" && isObj(part.input_audio) && isMedia(part.input_audio.data as Json))
    return <Media value={part.input_audio.data as Obj} pid={pid} />;
  if (t === "tool_use" || t === "tool_call" || t === "function")
    return (
      <div>
        <span className="badge accent">tool call</span> <b>{String(part.name ?? "")}</b>
        <JsonBlock value={(part.input ?? part.arguments ?? part) as Json} />
      </div>
    );
  if (t === "tool_result" || t === "tool_call_response")
    return (
      <div>
        <span className="badge">tool result</span>
        <JsonBlock value={(part.content ?? part.response ?? part) as Json} />
      </div>
    );
  if (t === "thinking")
    return (
      <details>
        <summary className="muted">thinking</summary>
        <pre>{String(part.thinking ?? "")}</pre>
      </details>
    );
  return <JsonBlock value={part} />;
}

function MessageList({ messages, pid }: { messages: Json[]; pid: string }) {
  return (
    <div>
      {messages.map((m, i) => {
        if (!isObj(m)) return <JsonBlock key={i} value={m} />;
        const role = String(m.role ?? "message");
        const content = (m.content ?? m.parts ?? null) as Json;
        const tools = m.tool_calls as Json;
        return (
          <div key={i} className={`msg ${role}`}>
            <div className="role">{role}</div>
            {Array.isArray(content) ? content.map((p, j) => <Part key={j} part={p} pid={pid} />) : <Part part={content} pid={pid} />}
            {Array.isArray(tools) && tools.length > 0 && (
              <div style={{ marginTop: 6 }}>
                <span className="badge accent">tool calls</span>
                <JsonBlock value={tools} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

const looksLikeMessages = (v: Json): v is Json[] =>
  Array.isArray(v) && v.length > 0 && v.every((m) => isObj(m) && "role" in m);

const looksLikeBlocks = (v: Json): v is Json[] =>
  Array.isArray(v) && v.length > 0 && v.every((m) => isObj(m) && typeof m.type === "string");

/** Smart viewer for span inputs and outputs, falling back to JSON. */
export function Content({ value, pid }: { value: Json; pid: string }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  if (typeof value === "string") return <pre className="prewrap">{value}</pre>;
  if (isMedia(value)) return <Media value={value} pid={pid} />;
  if (looksLikeMessages(value)) return <MessageList messages={value} pid={pid} />;
  if (looksLikeBlocks(value)) return <MessageList messages={[{ role: "assistant", content: value }]} pid={pid} />;
  if (isObj(value)) {
    if (looksLikeMessages(value.messages as Json)) {
      const rest = Object.fromEntries(Object.entries(value).filter(([k]) => k !== "messages"));
      const system = rest.system;
      delete rest.system;
      return (
        <div>
          {system !== undefined && <MessageList messages={[{ role: "system", content: system }]} pid={pid} />}
          <MessageList messages={value.messages as Json[]} pid={pid} />
          {Object.keys(rest).length > 0 && <JsonBlock value={rest} />}
        </div>
      );
    }
    if (isObj(value) && "role" in value && ("content" in value || "tool_calls" in value))
      return <MessageList messages={[value]} pid={pid} />;
    const media = Object.entries(value).filter(([, v]) => isMedia(v));
    if (media.length > 0) {
      const rest = Object.fromEntries(Object.entries(value).filter(([, v]) => !isMedia(v)));
      return (
        <div>
          {media.map(([k, v]) => (
            <div key={k} style={{ marginBottom: 8 }}>
              <div className="small muted">{k}</div>
              <Media value={v as Obj} pid={pid} />
            </div>
          ))}
          {Object.keys(rest).length > 0 && <Content value={rest} pid={pid} />}
        </div>
      );
    }
  }
  return <JsonBlock value={value} />;
}

export function JsonBlock({ value }: { value: Json }) {
  return <pre className="json">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}
