import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectPath, type DatasetSummary, type Json, type PlaygroundModels } from "../api";
import { usePid, useRole } from "../hooks";
import { fmtCost, fmtMs } from "../format";

interface Msg {
  role: string;
  content: string;
}
interface State {
  kind: "chat" | "text";
  messages: Msg[];
  text: string;
  model: string;
  params: Record<string, number>;
  promptName: string;
  promptVersion: number | null;
}

const VAR = /\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}/g;

function initialState(): State {
  const fresh: State = {
    kind: "chat",
    messages: [
      { role: "system", content: "You are a concise, accurate assistant." },
      { role: "user", content: "{{question}}" },
    ],
    text: "",
    model: "",
    params: { temperature: 0.2, max_tokens: 512 },
    promptName: "",
    promptVersion: null,
  };
  try {
    const raw = sessionStorage.getItem("tirkit.playground");
    if (!raw) return fresh;
    sessionStorage.removeItem("tirkit.playground");
    const s = JSON.parse(raw) as { kind?: string; template?: Json; model?: string; params?: Record<string, number>; promptName?: string; promptVersion?: number };
    const msgs = Array.isArray(s.template)
      ? (s.template as { role: string; content: Json }[]).map((m) => ({ role: m.role, content: typeof m.content === "string" ? m.content : JSON.stringify(m.content) }))
      : fresh.messages;
    return {
      ...fresh,
      kind: s.kind === "text" ? "text" : "chat",
      messages: msgs,
      text: typeof s.template === "string" ? s.template : "",
      model: s.model || "",
      params: { ...fresh.params, ...(s.params || {}) },
      promptName: s.promptName || "",
      promptVersion: s.promptVersion ?? null,
    };
  } catch {
    return fresh;
  }
}

export default function Playground() {
  const pid = usePid();
  const role = useRole();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [st, setSt] = useState<State>(initialState);
  const [vars, setVars] = useState<Record<string, string>>({});
  const [out, setOut] = useState<{ output: string; usage: Record<string, number>; cost_usd: number | null; latency_ms: number; trace_id: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [saveName, setSaveName] = useState(st.promptName);
  const [msg, setMsg] = useState("");
  const models = useQuery({
    queryKey: ["models", pid],
    queryFn: () => api<PlaygroundModels>(projectPath(pid, "/playground/models")),
  });
  useEffect(() => {
    if (!st.model && models.data?.models.length) setSt((s) => ({ ...s, model: models.data!.models[0] }));
  }, [models.data, st.model]);
  const template = (st.kind === "chat" ? st.messages : st.text) as unknown as Json;
  const variables = useMemo(() => {
    const text = st.kind === "chat" ? st.messages.map((m) => m.content).join("\n") : st.text;
    return [...new Set([...text.matchAll(VAR)].map((m) => m[1]))];
  }, [st]);
  const run = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = await api<NonNullable<typeof out>>(projectPath(pid, "/playground/run"), {
        method: "POST",
        json: { kind: st.kind, template, variables: vars, model: st.model, params: st.params, prompt_name: st.promptName || undefined, prompt_version: st.promptVersion || undefined },
      });
      setOut(r);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    setErr("");
    try {
      const r = await api<{ name: string; version: number }>(projectPath(pid, `/prompts/${encodeURIComponent(saveName)}/versions`), {
        method: "POST",
        json: { kind: st.kind, template, config: { model: st.model, params: st.params }, message: msg || null },
      });
      setSt({ ...st, promptName: r.name, promptVersion: r.version });
      void qc.invalidateQueries({ queryKey: ["prompts", pid] });
      setMsg("");
      nav(`/p/${pid}/prompts/${encodeURIComponent(r.name)}`);
    } catch (e) {
      setErr(String(e));
    }
  };
  const configured = models.data && (models.data.providers.openai_compatible?.configured || models.data.providers.anthropic?.configured);
  const setMsgAt = (i: number, patch: Partial<Msg>) => setSt({ ...st, messages: st.messages.map((m, j) => (j === i ? { ...m, ...patch } : m)) });
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Playground</h1>
          <div className="muted small">
            {st.promptName ? (
              <>
                Editing <b>{st.promptName}</b>
                {st.promptVersion ? ` (from v${st.promptVersion})` : ""}. Runs are traced in this project.
              </>
            ) : (
              "Write a prompt, try it on a model, save it as a versioned prompt, or run it over a dataset."
            )}
          </div>
        </div>
      </div>
      {models.data && !configured && (
        <div className="notice" style={{ marginBottom: 12 }}>
          No model provider is configured on the server. Set <code>LLM_BASE_URL</code> (any OpenAI-compatible endpoint, e.g. a local LM Studio at <code>http://localhost:1234/v1</code>) and{" "}
          <code>LLM_API_KEY</code>, or <code>ANTHROPIC_API_KEY</code>, then restart the server.
        </div>
      )}
      <div className="split">
        <div className="panel">
          <div className="row" style={{ marginBottom: 10 }}>
            <div className="seg">
              {(["chat", "text"] as const).map((k) => (
                <button key={k} className={st.kind === k ? "on" : ""} onClick={() => setSt({ ...st, kind: k })}>{k}</button>
              ))}
            </div>
            <input list="models" aria-label="Model" placeholder="model" value={st.model} onChange={(e) => setSt({ ...st, model: e.target.value })} style={{ flex: 1 }} />
            <datalist id="models">{models.data?.models.map((m) => <option key={m} value={m} />)}</datalist>
          </div>
          {st.kind === "chat" ? (
            <div className="grid" style={{ gap: 8 }}>
              {st.messages.map((m, i) => (
                <div key={i}>
                  <div className="row small">
                    <select value={m.role} onChange={(e) => setMsgAt(i, { role: e.target.value })} aria-label="Role">
                      <option>system</option>
                      <option>user</option>
                      <option>assistant</option>
                    </select>
                    <div className="spacer" />
                    <button onClick={() => setSt({ ...st, messages: st.messages.filter((_, j) => j !== i) })}>Remove</button>
                  </div>
                  <textarea rows={Math.min(10, Math.max(2, m.content.split("\n").length + 1))} value={m.content} onChange={(e) => setMsgAt(i, { content: e.target.value })} style={{ fontFamily: "inherit", marginTop: 4 }} />
                </div>
              ))}
              <button onClick={() => setSt({ ...st, messages: [...st.messages, { role: "user", content: "" }] })}>Add message</button>
            </div>
          ) : (
            <textarea rows={10} value={st.text} onChange={(e) => setSt({ ...st, text: e.target.value })} style={{ fontFamily: "inherit" }} />
          )}
          <div className="row" style={{ marginTop: 10 }}>
            <label className="small">temperature <input type="number" step="0.1" min={0} max={2} value={st.params.temperature ?? ""} onChange={(e) => setSt({ ...st, params: { ...st.params, temperature: Number(e.target.value) } })} style={{ width: 70 }} /></label>
            <label className="small">max tokens <input type="number" min={1} value={st.params.max_tokens ?? ""} onChange={(e) => setSt({ ...st, params: { ...st.params, max_tokens: Number(e.target.value) } })} style={{ width: 90 }} /></label>
          </div>
        </div>
        <div className="grid">
          <div className="panel">
            <h3>Variables</h3>
            {variables.length === 0 && <div className="small muted">Add <code>{"{{name}}"}</code> placeholders to the prompt.</div>}
            {variables.map((v) => (
              <div key={v} style={{ marginBottom: 6 }}>
                <div className="small mono">{v}</div>
                <textarea rows={2} value={vars[v] ?? ""} onChange={(e) => setVars({ ...vars, [v]: e.target.value })} style={{ fontFamily: "inherit" }} />
              </div>
            ))}
            <button className="primary" disabled={busy || !st.model} onClick={run}>{busy ? "Running…" : "Run"}</button>
          </div>
          {err && <div className="error-box">{err}</div>}
          {out && (
            <div className="panel">
              <div className="row small muted" style={{ marginBottom: 6 }}>
                <span>{fmtMs(out.latency_ms)}</span>·<span>{out.usage.input_tokens ?? 0} in / {out.usage.output_tokens ?? 0} out</span>·<span>{fmtCost(out.cost_usd)}</span>
                {out.trace_id && <Link to={`/p/${pid}/traces/${out.trace_id}`}>trace</Link>}
              </div>
              <div className="msg assistant">
                <div className="role">assistant</div>
                <pre>{out.output}</pre>
              </div>
            </div>
          )}
          {role !== "viewer" && (
            <>
              <div className="panel">
                <h3>Save as prompt version</h3>
                <div className="row">
                  <input placeholder="prompt name" value={saveName} onChange={(e) => setSaveName(e.target.value)} />
                  <input placeholder="what changed?" value={msg} onChange={(e) => setMsg(e.target.value)} style={{ flex: 1 }} />
                  <button className="primary" disabled={!saveName} onClick={save}>Save version</button>
                </div>
              </div>
              <RunOnDataset pid={pid} kind={st.kind} template={template} model={st.model} params={st.params} prompt={st.promptName && st.promptVersion ? { name: st.promptName, version: st.promptVersion } : null} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function RunOnDataset({ pid, kind, template, model, params, prompt }: { pid: string; kind: string; template: Json; model: string; params: Record<string, number>; prompt: { name: string; version: number } | null }) {
  const nav = useNavigate();
  const datasets = useQuery({ queryKey: ["datasets", pid], queryFn: () => api<DatasetSummary[]>(projectPath(pid, "/datasets")) });
  const [dataset, setDataset] = useState("");
  const [scorers, setScorers] = useState("exact_match_norm, contains_expected");
  const [judge, setJudge] = useState("");
  const [status, setStatus] = useState("");
  const start = async () => {
    setStatus("starting…");
    try {
      const specs = scorers.split(",").map((s) => s.trim()).filter(Boolean);
      const r = await api<{ job_id: string }>(projectPath(pid, "/experiments/run"), {
        method: "POST",
        json: { dataset, prompt: prompt ?? undefined, kind, template: prompt ? undefined : template, model, params, scorers: specs, judge_model: judge || undefined },
      });
      setStatus("running…");
      for (let i = 0; i < 600; i++) {
        await new Promise((res) => setTimeout(res, 1000));
        const job = await api<{ status: string; error: string | null; result: { experiment_id: string } | null }>(projectPath(pid, `/jobs/${r.job_id}`));
        if (job.status === "done" && job.result) {
          nav(`/p/${pid}/experiments/${job.result.experiment_id}`);
          return;
        }
        if (job.status === "failed") {
          setStatus(`failed: ${job.error?.split("\n")[0]}`);
          return;
        }
      }
    } catch (e) {
      setStatus(String(e));
    }
  };
  return (
    <div className="panel">
      <h3>Run over a dataset</h3>
      <div className="small muted" style={{ marginBottom: 6 }}>
        Each example's input fields fill the variables. Starts an experiment you can compare with others. Scorers: exact_match, exact_match_norm, contains_expected, json_valid, wer, cer, judge:correctness (needs a judge model).
      </div>
      <div className="row">
        <select value={dataset} onChange={(e) => setDataset(e.target.value)} aria-label="Dataset">
          <option value="">dataset…</option>
          {datasets.data?.map((d) => <option key={d.name} value={d.name}>{d.name} ({d.n_items})</option>)}
        </select>
        <input value={scorers} onChange={(e) => setScorers(e.target.value)} aria-label="Scorers" style={{ flex: 1 }} />
        <input placeholder="judge model (optional)" value={judge} onChange={(e) => setJudge(e.target.value)} />
        <button className="primary" disabled={!dataset || !model} onClick={start}>Run experiment</button>
      </div>
      {status && <div className="small muted" style={{ marginTop: 6 }}>{status}</div>}
    </div>
  );
}
